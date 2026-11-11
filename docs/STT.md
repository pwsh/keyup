# Speech-to-text

The worker transcribes calls through one or more **backends**: rows managed in Admin → Backends,
seeded on first start from `STT_BACKENDS` (a JSON list in `.env`; once the table has rows, the
database wins and the variable is not read again). The
[router](../backend/app/stt/router.py) sends each job to the healthy backend with the lowest
`priority` number that has a free slot (`concurrency`), and fails over to the next one when a
backend is down or saturated. Every backend is whisper; what differs is where it runs.

| | Where it runs | What it needs | Section |
|---|---|---|---|
| **CPU** | a [Speaches](https://github.com/speaches-ai/speaches) CPU container beside the stack | cores and memory, nothing else | [§1](#1-cpu-the-default) |
| **NVIDIA GPU** | a Speaches CUDA container, on this host or another | an NVIDIA driver and the NVIDIA Container Toolkit | [§2](#2-nvidia-gpu) |
| **Intel iGPU / NPU** | an OpenVINO container, started by a compose profile | an Intel Core Ultra-class CPU with its `/dev/dri` and `/dev/accel` nodes | [§3](#3-intel-igpu-and-npu) |

A dedicated transcription appliance (one OpenVINO Model Server serving both the iGPU and the
NPU of an Intel box, as its own container or VM) is packaged separately at
[github.com/pwsh/whisper-lxc](https://github.com/pwsh/whisper-lxc). [§3.10](#310-the-standalone-appliance)
explains why it exists.

## 1. CPU (the default)

Speaches serves faster-whisper behind the OpenAI Audio API, which the worker's
`openai_compatible` backend kind already speaks. `compose.yaml` carries it as the `stt-cpu`
service under the `stt-cpu` profile, and a new install starts with both turned on:
`.env.example` sets the profile, and the built-in backend (`whisper`) points at it.

```sh
# .env next to compose.yaml (the .env.example values)
COMPOSE_PROFILES=stt-cpu
STT_CPU_MODEL=Systran/faster-whisper-small
STT_BACKENDS=[{"name":"whisper","kind":"openai_compatible","base_url":"http://stt-cpu:8000","model":"Systran/faster-whisper-small","concurrency":2,"priority":10}]
```

```sh
docker compose up -d
docker compose ps stt-cpu             # "healthy" once the model is downloaded
curl -s localhost:8093/health         # STT_CPU_PORT, published on the loopback only
```

Speaches will not transcribe with a model it has not downloaded (it answers 404), so the
service's command asks it to fetch `STT_CPU_MODEL` into the `sttcpu` volume on every start;
that is a no-op once the model is there. The container reports healthy only when the model
is listed, the same check the worker's backend health makes, so `STT_CPU_MODEL` must be the
`model` of the backend row. The first start downloads about 500 MB for `faster-whisper-small`.

Sizing. The model runs as int8 (`STT_CPU_COMPUTE_TYPE`), holds about 1 GB of RAM, and keeps
the cores it is given busy while a call transcribes: a 4-second call took about 3 seconds on a
6-core desktop. On a small host set `STT_CPU_THREADS` so Postgres and ingest keep a core or
two, and leave the backend's `concurrency` at 1 or 2. Larger models are more accurate and
several times slower on a CPU, so measure on your own calls (`python -m app.stt.bench`) before
moving up; change `STT_CPU_MODEL` and the backend row's `model` together.

When transcription runs somewhere else (a GPU, §2, or an Intel accelerator, §3), take
`stt-cpu` out of `COMPOSE_PROFILES`, or keep it as the low-priority failover. Removing a
profile does not stop a container that is already running: `docker compose stop stt-cpu`.

Without the extra container, the worker can run faster-whisper in-process instead
(`{"kind":"faster_whisper_local","model":"Systran/faster-whisper-small"}`). That needs the
backend's `cpu` extra (`faster-whisper`), which the backend image does not install, and it takes
CPU from the worker itself, so the separate container is the better default.

## 2. NVIDIA GPU

A GPU is what makes the large models practical. It needs the NVIDIA driver and the NVIDIA
Container Toolkit on the host that has the card (`docker run --rm --gpus all ubuntu nvidia-smi`
must work first).

On the Keyup host itself, the optional `compose.gpu.yaml` adds an `stt-gpu` service (the Speaches
CUDA image, pinned like `stt-cpu`, downloading `STT_GPU_MODEL`, default
`Systran/faster-whisper-large-v3`, into its own volume):

```sh
# .env next to compose.yaml
COMPOSE_FILE=compose.yaml:compose.gpu.yaml
COMPOSE_PROFILES=stt-cpu,stt-gpu      # keep stt-cpu as the failover, or drop it
```

The worker then reaches it as `http://stt-gpu:8000`. On another machine, run the same image by
hand and use that machine's address instead:

```sh
docker run -d --gpus all -p 8000:8000 --restart unless-stopped \
  ghcr.io/speaches-ai/speaches:0.8.3-cuda
curl -X POST localhost:8000/v1/models/Systran/faster-whisper-large-v3   # download the model once
```

Either way, add it ahead of the CPU entry (Admin -> Backends, or `STT_BACKENDS` on a fresh
install):

```jsonc
STT_BACKENDS=[
  {"name":"gpu","kind":"openai_compatible","base_url":"http://stt-gpu:8000",
   "model":"Systran/faster-whisper-large-v3","concurrency":2,"priority":5},
  {"name":"whisper","kind":"openai_compatible","base_url":"http://stt-cpu:8000",
   "model":"Systran/faster-whisper-small","concurrency":2,"priority":10}
]
```

(`http://gpu.example.org:8000` instead of `http://stt-gpu:8000` for a GPU on another machine.)

The router uses the GPU while it is healthy and falls back to the CPU when it is down or busy, so
the GPU host can be rebooted without stopping transcription.

## 3. Intel iGPU and NPU

Everything below was measured on an Intel Core Ultra 7 258V (Lunar Lake), which has three
processors that can run whisper: the CPU, an **Arc 140V iGPU** at `/dev/dri/renderD128`, and an
**NPU** ("AI Boost") at `/dev/accel/accel0`. This section is how to point transcription at the
last two, what it buys, and what it does not.

The engine is [OpenVINO GenAI](https://github.com/openvinotoolkit/openvino.genai)'s
`WhisperPipeline`, wrapped as the `openvino_genai` backend kind in
[`backend/app/stt/openvino_local.py`](../backend/app/stt/openvino_local.py). `device` is
literally the string `CPU`, `GPU`, `NPU` or `AUTO`, so one class covers all three and the
[router](../backend/app/stt/router.py) picks between them by priority (lower number first) and health like any
other backend.

### 3.1 Two ways in, and which one you want

| | **In-process** (`openvino_genai`) | **Sidecar** (`stt-openvino` + `openai_compatible`) |
|---|---|---|
| Where the model runs | inside the worker process | inside its own container |
| What the worker imports | `openvino-genai`, `huggingface_hub` | nothing |
| Needs Intel userspace in the worker image | **yes** | no |
| Config | `{"kind":"openvino_genai","device":"GPU"}` | `{"kind":"openai_compatible","base_url":"http://stt-openvino:8000"}` |
| Use it when | a single-container / bare-metal deployment where the process already has the drivers | **compose — this is the supported path** |

The compose deployment uses the sidecar for one reason: the api/worker image is
`python:3.12-slim` and has none of the Intel userspace (the OpenCL ICD, Level Zero, the NPU
compiler) that OpenVINO needs to see an accelerator. Rather than move that stack into the
image everything else runs in, the accelerator gets its own container built from
[`backend/Dockerfile.openvino`](../backend/Dockerfile.openvino), and
[`backend/app/stt/ov_server.py`](../backend/app/stt/ov_server.py) serves the *same backend
class* over the OpenAI Audio API the worker already speaks. The worker gains an accelerator
without gaining a dependency.

```
worker ──HTTP──▶ stt-openvino container ──▶ OpenVINO ──▶ /dev/dri/renderD128 (Arc 140V)
  (slim image)     (openvino/ubuntu24_runtime)             /dev/accel/accel0   (AI Boost NPU)
```

---

### 3.2 The host side

The device nodes have to exist on the machine that runs Docker before the container can be
given them. On bare metal they do once the drivers are loaded. If Docker itself runs inside a
container or VM, pass `/dev/dri/renderD128` and `/dev/accel/accel0` through to it first, owned by
its `render` group (see your hypervisor's device passthrough documentation). The NPU's kernel
driver (`intel_vpu`, the accel/ivpu module — the kernel keeps the older "VPU" name) and the
GPU's (`i915`/`xe`) live in the **host** kernel, not in the container image: a container can
only use a device the host kernel has already brought up.

Verify on the machine that runs Docker:

```sh
ls -l /dev/dri /dev/accel
# crw-rw---- 1 root render 226, 128 … /dev/dri/renderD128
# crw-rw---- 1 root render 261,   0 … /dev/accel/accel0
getent group render        # -> render:x:992:
```

If the gid differs, set `RENDER_GID` in `.env` to whatever `ls -l` shows — that number is what
compose adds to the container's supplementary groups, and it is the whole of the permission
story. (Intel documents the same requirement for bare metal: the user must be in `render` or
`video` to open `/dev/accel/accel0`.)

### 3.3 Turning the `stt-openvino` profile on

```sh
# .env next to compose.yaml
COMPOSE_PROFILES=stt-openvino
OV_MODEL=OpenVINO/whisper-base-fp16-ov
OV_DEVICE=GPU
RENDER_GID=992
```

```sh
docker compose up -d stt-openvino
docker compose logs -f stt-openvino     # first start downloads the model and compiles it
curl -s localhost:8098/health | jq
# {"ok":true,"detail":"OpenVINO/whisper-base-fp16-ov ready on GPU (compiled in 4.9s); available devices: CPU, GPU, NPU",
#  "model":"…","device":"GPU","devices":["CPU","GPU","NPU"]}
```

`devices` is the answer straight out of `openvino.Core().available_devices`, and it is the
fastest way to tell a permissions problem (`["CPU"]` — the container cannot open the device
node) from a driver problem (an error in `detail`). The same string reaches the admin backends
page through the router's health report.

Without an NPU in the machine, comment the `/dev/accel/accel0` line out of the `stt-openvino`
service: compose refuses to start a service whose device node does not exist.

The model and its **compiled blob cache** live in the `ovmodels` volume. Keep it: compiling
whisper for the GPU takes seconds and for the NPU substantially longer, and the cache is what
you pay it only once for.

### 3.4 Wiring it into the router

Add one ordinary `openai_compatible` entry to `STT_BACKENDS`, between the GPU box and the CPU
fallback:

```jsonc
STT_BACKENDS=[
  {"name":"gpu","kind":"openai_compatible","base_url":"http://gpu.example.org:8000",
   "model":"Systran/faster-whisper-large-v3","concurrency":2,"priority":10},
  {"name":"stt-openvino","kind":"openai_compatible","base_url":"http://stt-openvino:8000",
   "model":"OpenVINO/whisper-base-fp16-ov","concurrency":1,"priority":50},
  {"name":"local","kind":"faster_whisper_local","model":"Systran/faster-whisper-small",
   "concurrency":1,"priority":90}
]
```

One more reason the sidecar is the supported path: the health probe for an in-process
`openvino_genai` entry *compiles the model* — deliberately, because a model that cannot compile
for this device is what the router needs to learn before it sends a job there, but it means the
first probe blocks for as long as the compile takes. That now happens in the **worker**, which
is the process that has a router; the admin page reads the published verdict from
`stt_backend_stats` and never probes anything itself (§5.2). Through the sidecar the probe is
one HTTP request either way.

`model` must be the exact string the sidecar serves (`OV_MODEL`): the client's health probe
looks for it in `GET /v1/models`, and a mismatch answers 404 — which is deliberately the
signal that makes the router *disqualify* the backend rather than retry it forever.

> **Priority, precisely.** `BackendRouter._candidates` sorts by `priority` ascending, so the
> **lowest** number wins among healthy backends, matching `config.py`.
> 50 sits between the other two: the GPU box (10) is tried first because it is the better
> transcriber, the accelerator takes over when the GPU box is down or saturated, and the
> CPU fallback (90) is the last resort.

For a single-container or bare-metal deployment where the process itself has the drivers, skip
the sidecar and name the kind directly — nothing else changes:

```jsonc
{"name":"npu","kind":"openvino_genai","model":"OpenVINO/whisper-large-v3-turbo-int8-ov",
 "device":"NPU","concurrency":1,"priority":50}
```

Install the wheels with `pip install 'keyup[openvino]'` (two of them: `openvino-genai` and
`huggingface_hub`; audio is decoded by the `ffmpeg` Keyup already requires).

### 3.5 What it is *not* doing: VAD

openvino-genai has no `vad_filter`. That matters here more than it would elsewhere — 14 % of
the development corpus is under a second of squelch tail, and in the reference faster-whisper
run a 0.42 s tail decoded as **"Thanks for watching!"** with VAD off and to empty text with it on.

The stand-in is two-sided, and neither half is whisper's VAD:

* **in front of the decoder** — a clip shorter than 0.75 s (the same threshold ingest uses to
  decide whether to queue a job) or quieter than an RMS of 0.0015 is never decoded at all. It
  returns empty text, which `postprocess.assess` labels `noise` for the reason `empty_text`.
  Measured on this corpus: real traffic sits at RMS 0.02–0.20, that squelch tail at ~0.0008.
* **behind it** — the existing hallucination heuristics, unchanged (including the caption-credit and sign-off
  pattern families and the `stt_hallucination_patterns` setting; ARCHITECTURE §5.2).

Two of those heuristics lose their input: openvino-genai reports no `avg_logprob` and no
`no_speech_prob`, so the low-confidence and all-non-speech checks never fire (`assess` filters
`None` out before comparing, so they simply do not vote). `compression_ratio` is recomputed
per segment from the segment text, so the repeat-loop check still works — which matters,
because the failure below is exactly a loop.

**Hotwords are off by default, and that default is measured.** openvino-genai injects
`initial_prompt` into the first 30 s window but `hotwords` into *every* window, and on short
clips the decoder just continues the list. Over the 15-file sample, on
whisper-large-v3-turbo-int8:

| | throughput | good | suspect | noise |
|---|---|---|---|---|
| no prompt | 2.8× | 12 | 1 | 2 |
| `initial_prompt` only *(the default)* | 2.0× | 11 | 2 | 2 |
| `initial_prompt` + `hotwords` | 1.2× | 7 | 6 | 2 |

The six suspects in the last row are all the hotword list read back: a run of local place
names, repeating until the token limit. Set
`"use_hotwords": true` on the backend entry if a future model tolerates them; `"use_prompt":
false` turns the domain prompt off too.

### 3.6 Measured performance

**Caveat: these numbers are from the development laptop (11th-gen i7-1185G7, Iris Xe iGPU),
not from the 258V.** They are useful for the *shape* of the answer — which model,
GPU versus CPU, where quality falls over — and not as a forecast for Lunar Lake, whose Arc
140V is a much larger GPU and whose NPU is not present here at all.

15 duration-stratified corpus files (82.2 s of audio, 0.24 s to 44.5 s), domain prompt on,
hotwords off, `python -m app.stt.bench --backend openvino --device …`, run inside the
`stt-openvino` image:

| model | device | wall | throughput | good | suspect | noise |
|---|---|---|---|---|---|---|
| `whisper-base-fp16-ov` | CPU (4 threads) | 9.2 s | **8.9×** | 13 | 1 | 1 |
| `whisper-base-fp16-ov` | GPU | 6.8 s | **12.1×** | 13 | 1 | 1 |
| `whisper-small-fp16-ov` | GPU | 12.3 s | 6.7× | 11 | 1 | 3 |
| `whisper-medium-fp16-ov` | GPU | 26.5 s | 3.1× | **14** | 0 | 1 |
| `whisper-large-v3-turbo-int8-ov` | CPU (4 threads) | 76.1 s | 1.1× | 11 | 2 | 2 |
| `whisper-large-v3-turbo-int8-ov` | GPU | 39.9 s | 2.1× | 11 | 2 | 2 |

Throughput is audio seconds per wall-clock second over the decoded clips. The iGPU is worth
roughly 1.4–2× over four CPU threads on this laptop, and it does it without taking the cores
the ingest worker and Postgres are using — which on a single-box deployment is the real
argument for it, more than the raw factor.

Model compile time (cached afterwards in `ovmodels`): base 0.9 s CPU / 4.9 s GPU, turbo-int8
64.7 s CPU / 7.9 s GPU on first run.

**Against a GPU reference** (faster-whisper `large-v3` on a separate GPU server), on the five
files that reference run captured (transcripts paraphrased here, since they name real addresses):

| clip | GPU `large-v3` | OV `turbo-int8` (GPU) | OV `base-fp16` (GPU) |
|---|---|---|---|
| 4.91 s | correct street directions | same as the reference | two words misheard, street name right |
| 14.99 s | unit, house number, street and patient age all correct | same as the reference | unit and street garbled, age wrong |
| 2.04 s | a short reply, correct | a different, wrong phrase | same as the reference |
| 30.30 s | a two-department fire dispatch, plus a hallucinated *"Thank you for watching."* | **"The End Thank you."** | the dispatch, correct and complete |
| 0.42 s squelch | "" (with `vad_filter`) | "" (guard) | "" (guard) |

Read that table before choosing a model. `large-v3-turbo-int8` matches the reference on the
clips it gets right — it is the only OpenVINO model here that hears the unit and street of the
14.99 s call — but on the 30.3 s fire dispatch it collapses to `"The End Thank you."`, on both CPU
and GPU, with or without the prompt, with or without `max_new_tokens`. It is the int8 model
itself, not the wiring, and whisper-base transcribes that same clip correctly. Until that is
understood, the accelerator is a *supplement* to a GPU server rather than a replacement for it,
and `whisper-base-fp16-ov` (fast, no catastrophic failures observed) or
`whisper-medium-fp16-ov` (best labels in the sample, 3.1×) is the safer `OV_MODEL`.

### 3.7 NPU caveats

The NPU is the least proven path here, and this section separates what Intel documents from
what is inferred. **Through openvino-genai, nothing on the NPU worked on the 258V beyond device
enumeration** (see the measurements below) — `openvino.Core().available_devices` reports
`['CPU', 'GPU', 'NPU']` inside the container with both nodes passed in, and the NPU reports `FULL_DEVICE_NAME` "Intel(R) AI Boost". No
transcription has been run on it.

**Documented**

* `WhisperPipeline` supports `device="NPU"`; whisper on NPU arrived in OpenVINO 2024.5, and
  there are "no NPU-specific requirements when running the Whisper GenAI pipeline on NPU"
  ([GenAI on NPU](https://docs.openvino.ai/2026/openvino-workflow-generative/inference-with-genai/inference-with-genai-on-npu.html)).
* The NPU plugin runs **static shapes only**
  ([NPU device](https://docs.openvino.ai/2026/openvino-workflow/running-inference/inference-devices-and-modes/npu-device.html)),
  and the pipeline handles that itself — it reshapes the encoder to a static batch of 1 and
  compiles the decoder through NPUW. Nothing in this repo has to do it.
* `--disable-stateful` exports were required **before** OpenVINO 2025.1 and are **removed** in
  2026: "the deprecated OpenVINO GenAI support for whisper stateless decoder model has been
  removed. Please use a stateful model." Any model card telling you to export with
  `--disable-stateful` — including the third-party `FluidInference/*-ov-npu` repos — is stale
  for the 2026.3 runtime this image ships.
* Model sizes named in the NPU docs: whisper **tiny, base, small, large** (the 2024.5 notes
  say base, small, medium — the two lists disagree and neither claims to be exhaustive).
  Weights may stay FP16 or be compressed to INT8; the INT4/symmetric-quantization requirement
  documented for NPU applies to LLMs, not to whisper.
* Compilation for the NPU is slow enough that Intel documents caching as the remedy, and the
  NPU plugin does support importing a cached compiled model (`CACHE_DIR`, which this backend
  always sets, plus the driver's own UMD cache). Blob compatibility across OpenVINO versions
  is explicitly *not* guaranteed — which is why the image pins the runtime and the wheel to
  one release train.
* Linux needs kernel 6.6+ with `intel_vpu`, and the userspace trio `intel-fw-npu`,
  `intel-level-zero-npu`, `intel-driver-compiler-npu` — all three are already in
  `openvino/ubuntu24_runtime:2026.3.1` (1.32.1.20260422), so the image installs no drivers of
  its own.

**Known bad, on this exact SKU**

* [openvino.genai#4222](https://github.com/openvinotoolkit/openvino.genai/issues/4222) (open):
  on a Core Ultra 258V, `WhisperPipeline(..., "NPU")` constructs and then throws
  `invalid unordered_map<K,T> key` on the first `generate()`, with the same IR working on CPU.
* [openvino.genai#1965](https://github.com/openvinotoolkit/openvino.genai/issues/1965)
  (closed, no published root cause): whisper-large-v3-turbo INT8 hanging on the NPU of a Core
  Ultra 7 258V, while whisper-base was fine.

**Unverified — do not plan around these**

* First-run NPU compile time. No official figure exists for any whisper size; the 300 s
  `start-period` on the container healthcheck is a guess with headroom, not a measurement.
* Whether the NPU is faster or slower than the Arc 140V for these clip lengths. Intel
  publishes iGPU generative benchmarks for the 258V but no NPU comparison, and the third-party
  claims either way are not Intel positions.
* `large-v3-turbo` on the NPU at all: it appears in no NPU support list, and one of the two
  reports above is precisely that model hanging.

**So the plan is:** `OV_DEVICE=GPU`. Try the NPU by flipping `OV_DEVICE=NPU` on
the sidecar and watching `/health` — a failure there is contained (the container reports
unhealthy, the router keeps using the other backends, and no job is lost) — and
start with `whisper-base-fp16-ov`, which is the size the NPU docs actually name.

### 3.8 Checking it worked

```sh
# the sidecar is up, on the device you asked for
curl -s localhost:8098/health | jq '{ok, device, devices, detail}'

# it transcribes a real call
curl -s -F 'file=@/srv/keyup/audio/<date>/<talkgroup>/<call>.mp3' \
        -F 'model=OpenVINO/whisper-base-fp16-ov' -F 'language=en' \
        localhost:8098/v1/audio/transcriptions | jq '{text, duration}'

# what the worker has published about it: health, throughput, in-flight, last error
curl -s -H "Authorization: Bearer $KEYUP_ADMIN_TOKEN" \
     localhost:8096/api/admin/stt/backends \
  | jq '.backends[] | {name, healthy, stale, in_flight, mean_rtf, last_error}'
docker compose logs worker | grep stt-openvino
```

Benchmark it against the corpus the same way every other backend is benchmarked:

```sh
docker compose exec stt-openvino \
  python3 -m app.stt.bench --backend openvino --device GPU \
    --model OpenVINO/whisper-base-fp16-ov --cache-dir /models \
    --audio-root /srv/keyup/audio --files 40
```

### Exporting a model that has no OpenVINO repo

`OV_MODEL` can be a directory under `/models` instead of a repo id. `optimum-intel` is a
build-time tool and is deliberately not a runtime dependency, so export elsewhere and copy the
directory into the volume:

```sh
pip install 'optimum-intel[openvino]'
optimum-cli export openvino --model openai/whisper-large-v3-turbo \
    --weight-format int8 whisper-large-v3-turbo-int8-ov
docker cp whisper-large-v3-turbo-int8-ov keyup-stt-openvino-1:/models/
# then: OV_MODEL=/models/whisper-large-v3-turbo-int8-ov
```

A directory with no `openvino_encoder_model.xml` in it is rejected at load with that command
line in the error message, rather than failing somewhere deeper.

### Measured on a Core Ultra 7 258V (2026-08-30)

Same 15 stratified corpus files (82.2 s of audio), `python -m app.stt.bench`, prompt on / hotwords off,
`openvino/ubuntu24_runtime:2026.3.1`. "overall" = audio seconds ÷ wall seconds including VAD-empty clips.

| backend | model | overall | decoded RTF mean / median | quality good/suspect/noise |
|---|---|---|---|---|
| remote GPU server (reference) | faster-whisper large-v3 | 12.3× | 8.6× / 7.0× | 10 / 0 / 5 |
| **Arc 140V iGPU** | whisper-base-fp16 | **24.4×** | 21.3× / 17.0× | 13 / 1 / 1 (but audibly wrong words, e.g. "WAN-IQ" for "wanted") |
| **Arc 140V iGPU** | **whisper-medium-fp16** | **7.2×** | 6.3× / 4.4× | **14 / 0 / 1** |
| Arc 140V iGPU | large-v3-turbo-int8 | 7.6× | 6.5× / 4.2× | 11 / 2 / 2 |
| NPU (AI Boost) | base / medium / turbo | — | — | **fails on every file** |

NPU failure signature (all three models, openvino-genai 2026.3):
`RuntimeError: Exception from src/inference/src/cpp/infer_request.cpp:246: Check '*roi_end <= *max_dim' failed at
src/inference/src/dev/make_tensor.cpp:35` — the ROI tensor path in the NPU Whisper pipeline, matching the upstream
reports cited above. Re-test after an openvino-genai upgrade; until then `OV_DEVICE=NPU` is not usable here.

**Recommended:** `OV_DEVICE=GPU`, `OV_MODEL=OpenVINO/whisper-medium-fp16-ov`, registered in `STT_BACKENDS` at
priority 50 — a GPU server (10), if you have one, stays primary for accuracy; the iGPU takes over automatically when
it is down or saturated, and a CPU backend (90) remains the last resort.


---

### 3.9 OpenVINO Model Server (Intel's official server)

Everything above runs OpenVINO GenAI *ourselves*, in a container we build. Intel also ships a
finished inference server, [OpenVINO Model Server](https://github.com/openvinotoolkit/model_server)
(OVMS), and on this box it does the one thing our sidecar cannot: **it transcribes on the NPU.**
`openvino-genai` 2026.3 throws `Check '*roi_end <= *max_dim' failed` on every clip on the 258V's
AI Boost; OVMS, given the same pre-converted model, decodes on it at 12.4x real time.

It is not a drop-in replacement for the `stt-openvino` sidecar — it is a much thinner API — so
it is a **separate backend kind**, `ovms`
([`backend/app/stt/ovms.py`](../backend/app/stt/ovms.py)), not another `openai_compatible`
entry. Both profiles can run at once; the router picks between them by priority and health.

#### What works

* **`openvino/model_server:weekly`** (built 2026-08-21), on **both** the Arc 140V iGPU
  (`--target_device GPU`) and the Lunar Lake NPU (`--target_device NPU`), with
  `--device /dev/dri/renderD128 --device /dev/accel/accel0 --group-add 992` — the same device
  nodes and the same gid the `stt-openvino` profile needs (§3.2; nothing extra on the host).
* **Pre-converted models from the `OpenVINO/` HF org**, named with `--source_model`. OVMS
  downloads the repo into `--model_repository_path` on first start and writes a `graph.pbtxt`
  beside it.
* `GET /v1/models` answers in the OpenAI shape, so the health probe this repo already had works
  unchanged: the backend is healthy when that list contains the configured model name.

#### What does not

* **`openvino/model_server:latest` and `:latest-gpu`** (July builds) do not work on this
  platform at all: the GPU plugin fails in `contexts.count(device_id)`, and the NPU compiler
  answers "unable to compile on the given platform". Pin the weekly build — by digest, since
  `weekly` itself moves every week (`compose.yaml` pins
  `weekly@sha256:de1d2eed…`, the 2026-08-21 build above). The release tag `2026.3` is that
  same failing July build (`sha256:069a1c11…`), so it is not a pin either.
* **In-server conversion.** `:latest-gpu` converting `openai/whisper-large-v3-turbo` from the
  transformers checkpoint produced no model. Use a repo that is already OpenVINO IR.
* **Sharing one model repository between two servers.** OVMS bakes `target_device:` into the
  `graph.pbtxt` it writes in the model directory, and rewrites that file on *every* start —
  measured: a second server started against a running one's repository flipped the file from
  `CPU` to `NPU` underneath it. On a simultaneous restart of both, the loser silently runs on
  the other's device. Hence two volumes, `ovmsnpu` and `ovmsgpu`, one per service.

#### Where it is thinner than our sidecar

`POST /v3/audio/transcriptions` — note `/v3`, not `/v1`. Multipart. Measured field by field
against the weekly image, because the failure mode here is *silence*, not an error:

| field | OVMS |
|---|---|
| `model` | **required**; the `--model_name`, not the repo id. A name it does not serve → **404** ("Mediapipe graph definition with requested name is not found") |
| `file` | **mp3 or wav only.** Anything else → 400 "Received input file is not valid wav nor mp3 audio file" |
| `language` | honoured |
| `temperature` | honoured — and **defaults to 1.0** (sampling) when the field is absent, which measurably changes the text |
| `timestamp_granularities[]=word` | 400 "Word timestamps not supported for this model" unless the model was exported with `enable_word_timestamps` |
| `timestamp_granularities[]=segment` | accepted, and it returns real `segments` — but it also *changed the decode* on `whisper-base-fp16-ov`, so this backend never asks for it |
| `prompt`, `response_format`, `hotwords`, `vad_filter` | **accepted and silently ignored.** No error, identical answer |

That last row is the one that matters. Our `openai_compatible` backend sends a domain prompt
assembled from talkgroup aliases, unit ids and place names, and Speaches uses it. OVMS takes
the same field, drops it, and answers exactly as if it had never been sent — so the `ovms`
backend does not send it at all, rather than leaving a prompt in the request log that never
reached a decoder. **On this path there is no domain prompt and no hotword biasing.**

Three more consequences, all handled in `ovms.py`:

* **No `verbose_json`.** The answer is `{"text": "..."}` and nothing else — no segments, no
  `avg_logprob`, no `no_speech_prob`, no `duration`. The transcript becomes **one segment
  spanning the whole call**, whose length comes from `ffprobe` on the recording.
  `compression_ratio` is recomputed from the text with zlib exactly as
  `openvino_local.py` does it, so the repeat-loop check still fires; the low-confidence and
  all-non-speech checks have no input and do not vote. `postprocess.assess` says so on the row,
  with a `no_model_metrics` reason — an OVMS `good` is a `good` from the text heuristics alone.
* **No VAD.** Same problem as openvino-genai, same answer: a clip shorter than 0.75 s never
  leaves the worker. (The RMS half of the guard is not available here — that needs decoded
  samples, and this backend deliberately does not decode.)
* **mp3/wav only.** trunk-recorder writes m4a by default (`TR_PREFER_AUDIO=m4a`, §1.7), so
  anything that is not already mp3 or wav is transcoded to 16 kHz mono wav with an `ffmpeg`
  subprocess before the upload.

#### Running it by hand

```sh
docker run -d --device /dev/dri/renderD128 --device /dev/accel/accel0 --group-add 992 \
  -p 127.0.0.1:8097:8000 -v /srv/keyup/ovms:/models:rw \
  openvino/model_server:weekly --rest_port 8000 \
  --source_model OpenVINO/whisper-large-v3-turbo-fp16-ov \
  --model_name whisper-large-v3-turbo-fp16-ov \
  --model_repository_path /models --cache_dir /models/.ovms_cache \
  --target_device NPU

curl -s localhost:8097/v1/models                      # readiness: the model name is listed
curl -s -F 'model=whisper-large-v3-turbo-fp16-ov' -F 'language=en' -F 'temperature=0' \
        -F 'file=@/srv/keyup/audio/<date>/<talkgroup>/<call>.mp3' \
        localhost:8097/v3/audio/transcriptions
```

`--cache_dir` is not optional in a container. OVMS defaults the compiled-model cache to
`/opt/cache` **inside the image**, so without pointing it into the volume every restart repays
the compile — 95 s for large-v3-turbo on the NPU, 287 s for full large-v3.

#### Turning the `stt-ovms` profile on

> **On a box with both an iGPU and an NPU, the standalone appliance ([§3.10](#310-the-standalone-appliance))
> is the better layout**: one server for both devices. This two-container profile still works and
> is the simplest way to run OVMS on the same machine as Keyup.

```sh
# .env next to compose.yaml
COMPOSE_PROFILES=stt-ovms,stt-openvino
OVMS_MODEL=OpenVINO/whisper-large-v3-turbo-fp16-ov
RENDER_GID=992
```

```sh
docker compose up -d stt-ovms-npu stt-ovms-gpu
docker compose logs -f stt-ovms-npu    # first start downloads the model and compiles it
curl -s localhost:8097/v1/models       # NPU;  8094 is the GPU container
```

The profile is two services — `stt-ovms-npu` and `stt-ovms-gpu` — sharing `OVMS_MODEL` and
nothing else. Their healthchecks have a 360 s `start_period` for that first compile, and the
`ovmsnpu` / `ovmsgpu` volumes are what stop you paying it twice. Register them with `model`
set to the **served** name (the basename of `OVMS_MODEL`):

```jsonc
  {"name":"ovms-npu","kind":"ovms","base_url":"http://stt-ovms-npu:8000",
   "model":"whisper-large-v3-turbo-fp16-ov","concurrency":1,"priority":40},
  {"name":"ovms-gpu","kind":"ovms","base_url":"http://stt-ovms-gpu:8000",
   "model":"whisper-large-v3-turbo-fp16-ov","concurrency":1,"priority":45}
```

#### Measured

Measured 2026-08-31 on a Core Ultra 7 258V via OVMS weekly built 2026-08-21, over the
same 15 stratified corpus files (94.1 s of audio), `POST /v3/audio/transcriptions`,
`temperature=0`. Throughput is audio seconds per wall-clock second.

| device | model | throughput | first-start compile | quality |
|---|---|---|---|---|
| **Arc 140V iGPU** | **large-v3-turbo-fp16** | **20.0x** | — | best of the set |
| **NPU (AI Boost)** | **large-v3-turbo-fp16** | **12.4x** | 95 s | best of the set |
| Arc 140V iGPU | medium-fp16 | 13.2x | — | clearly worse than turbo |
| NPU (AI Boost) | medium-fp16 | 10.1x | 65 s | clearly worse than turbo |
| NPU (AI Boost) | large-v3-fp16 | 5.8x | 287 s | no better than turbo |
| NPU (AI Boost) | base-fp16 | 46x | — | garbage |

`large-v3-turbo-fp16` is the choice on both devices: it is the most accurate model here *and*
faster than `medium`, and the full `large-v3` buys nothing for half the speed and three times
the compile. The iGPU is the faster of the two devices by ~1.6x, but the NPU is real capacity
that costs neither the GPU nor the CPU cores ingest and Postgres are using — which is the whole
argument for running both containers and letting the router use them in turn.

Against the numbers earlier in this document: the `stt-openvino` sidecar's best was
`whisper-medium-fp16` on the iGPU at 7.2x, and the NPU was unusable at any size. OVMS is faster
on the iGPU, better on model choice, and is the only way to use the NPU at all — paid for with
the missing domain prompt and the missing confidence metrics described above.

#### Testing it

The unit tests ([`backend/tests/stt/test_ovms.py`](../backend/tests/stt/test_ovms.py)) pin the
request shape as an assertion about OVMS rather than about us: `prompt`, `response_format`,
`hotwords` and `vad_filter` must be **absent**, and `temperature` must be **present**.

The live test is device-agnostic — the server decides the device, not the client — so the same
seven cases run against a CPU container on a laptop and against an NPU container:

```sh
docker run --rm -p 127.0.0.1:8097:8000 -v /tmp/ovms-models:/models:rw \
  openvino/model_server:weekly --rest_port 8000 \
  --source_model OpenVINO/whisper-base-fp16-ov --model_name whisper-base-fp16-ov \
  --model_repository_path /models --target_device CPU

STT_LIVE_OVMS=1 STT_LIVE_OVMS_MODEL=whisper-base-fp16-ov \
  backend/.venv/bin/pytest backend/tests/stt/test_live_ovms.py -m live -v
```


---

### 3.10 The standalone appliance

The layout these measurements led to: transcription moved off the Keyup host into its own
container on the same Intel box, one OVMS process serving both accelerators, reached by the
worker over HTTP. The reusable kit that builds it, and its deployment guide, are at
[github.com/pwsh/whisper-lxc](https://github.com/pwsh/whisper-lxc). What follows is only what
changed in the *understanding*; the how-to lives there.

#### One server, both devices

The two-container layout above exists because of a bug-shaped constraint: OVMS in
`--source_model` mode rewrites `graph.pbtxt` — the file carrying `target_device:` — on **every
start**, so two servers pointed at one model repository race for it and the loser silently
runs on the other's device. Two servers, two volumes, two ports, two copies of the weights.

In `--config_path` mode it does **not** rewrite them. That turns the constraint inside out.
A device is a property of a *directory*, so:

```
/models/OpenVINO/whisper-large-v3-turbo-fp16-ov/   the weights, once (1.5 GB)
/models/graphs/whisper-large-v3-turbo-gpu/         68 KB of symlinks + graph.pbtxt: GPU
/models/graphs/whisper-large-v3-turbo-npu/         68 KB of symlinks + graph.pbtxt: NPU
/models/config.json                                one mediapipe entry per servable
```

and **one process serves both**, on one port, choosing per request from the `model` field:
`-F model=whisper-large-v3-turbo-gpu` against `-F model=whisper-large-v3-turbo-npu`. OVMS
follows the symlinks (verified), and `ovms --configure` — the server's own tool — writes each
graph, so the thing writing the file is the thing reading it. Verified across a start and a
restart that both files still read `"GPU"` and `"NPU"`.

Six servables (turbo and medium, each on both devices, plus `whisper-gpu`/`whisper-npu`
aliases) load into one 5 GB process. Cold compile 220 s; warm restart from the cache 5-20 s.

#### The measurements that changed

Re-measured 2026-08-31 on the appliance container (8 cores / 12 GB), same 15 stratified files (94.1 s of
audio), same weekly image, `temperature=0`, **with nothing else on the node holding an
accelerator**.

| model | device | throughput |
|---|---|---|
| `large-v3-turbo-fp16` | **GPU** | **24.7x** |
| `large-v3-turbo-fp16` | **NPU** | **13.1x** |
| `medium-fp16` | GPU | 13.8x |
| `medium-fp16` | NPU | 10.2x |

The iGPU is better than the 20.0x recorded earlier in this document, not worse — but only
once it has the device to itself, and that turned out to be the finding worth writing down.

**An idle container cost 35 % of the iGPU.** Between deploying the appliance and retiring the
old `stt-ovms-gpu`/`stt-ovms-npu` containers on the Keyup host, both had `/dev/dri/renderD128`
open. The old containers were serving nothing — no jobs, no requests, healthy and asleep. In
that state the appliance benchmarked at **16.0x**; with them removed, the identical run gave
**24.7x**. The NPU path was unaffected (13.1x either way). This is almost certainly what the
earlier same-host numbers in this document were measuring. Turning a compose profile *off*
does not stop what it started, and `--remove-orphans` will not either, so after removing a
profile from `COMPOSE_PROFILES` stop its services explicitly
(`docker compose --profile stt-ovms stop stt-ovms-npu stt-ovms-gpu`).

**Cores help the iGPU path, and only it.** Whisper's front half — mp3 decode, mel spectrogram,
tokenisation — runs on the CPU on the way to the accelerator:

| cores | turbo GPU | turbo NPU |
|---|---|---|
| 4 | 19.9x | 12.6x |
| **8** | **24.7x** | 12.7x |

26 % for the iGPU, nothing for the NPU, which spends proportionally more of each request
inside the accelerator. Eight cores and 12 GB are the appliance's defaults for this reason
(8 GB also puts a four-servable config into swap).

#### The claim that did not survive

This document has argued that "the NPU is real capacity that costs neither the GPU nor the CPU
cores ... which is the whole argument for running both containers". **That is wrong, and it is
now measured.**

Both devices *do* execute at once — proved on the host kernel's own counters, which the guest
cannot fake (`npu_busy_time_us`, and the `xe` driver's `gtidle/idle_residency_ms`). Against an
idle baseline of NPU 0 ms busy / GPU 99.9 % idle:

| the only thing that differed | wall | NPU busy | GPU busy |
|---|---|---|---|
| `-F model=whisper-large-v3-turbo-gpu` | 5.1 s | **0 ms — 0 %** | **3.5 s — 68 %** |
| `-F model=whisper-large-v3-turbo-npu` | 8.4 s | **6.8 s — 80 %** | **0 ms — 0 %** |

A clean mirror image from one process on one port — which is also the proof that per-request
device switching works at all. Under simultaneous load both counters climb together: NPU 89 %
and GPU 57 % busy over the same 28 s window.

But executing at once is not the same as adding capacity. 188.2 s of audio:

| arrangement | wall | aggregate |
|---|---|---|
| **GPU alone, 1 request in flight** | **7.5 s** | **25.1x** |
| GPU + NPU, 1 request in flight each | 9.3 s | 20.3x |
| GPU alone, 4 requests in flight | 10.9 s | 17.3x |
| GPU + NPU, 4 requests in flight each | 13.0 s | 14.5x |
| NPU alone, 1 request in flight | 14.8 s | 12.7x |
| NPU alone, 4 requests in flight | 16.2 s | 11.6x |

The fastest arrangement is the simplest one. Sending work to the NPU *as well* makes the box
slower than sending all of it to the iGPU — 20.3x against 25.1x — and so does raising
concurrency on either device. Same cause in two places: on Lunar Lake the iGPU and the NPU
share one LPDDR5X memory subsystem, and the graphs carry `NUM_STREAMS: 1`, so parallel
requests to one servable queue behind each other (at 4-deep, 5x the mean latency for no
throughput).

So the NPU is a **failover lane, not extra capacity** — somewhere to send work when the iGPU
is unavailable, not a second engine to run beside it. `concurrency: 1` on both backends, and
prefer the iGPU.

#### Wiring the appliance into the router

```jsonc
STT_BACKENDS=[
  {"name":"gpu","kind":"openai_compatible","base_url":"http://gpu.example.org:8000",
   "model":"Systran/faster-whisper-large-v3","concurrency":4,"priority":10},
  {"name":"whisper-gpu","kind":"ovms","base_url":"http://192.0.2.221:8000",
   "model":"whisper-large-v3-turbo-gpu","concurrency":1,"priority":40},
  {"name":"whisper-npu","kind":"ovms","base_url":"http://192.0.2.221:8000",
   "model":"whisper-large-v3-turbo-npu","concurrency":1,"priority":45},
  {"name":"local-cpu","kind":"faster_whisper_local","model":"small",
   "concurrency":1,"priority":90,"enabled":false}
]
```

With the appliance doing the work, `COMPOSE_PROFILES` on the Keyup host needs neither OVMS
service nor the `stt-openvino` sidecar. The `ovms` backend kind, `backend/app/stt/ovms.py`, is
the same either way: it was always talking to a URL, and the URL is now on another machine.

The iGPU lane leads (40) because the measurements above make it the faster one by 1.9x (24.7x
against 13.1x), and using both at once is slower than using the iGPU alone; the NPU (45) is the
failover. `concurrency: 1` on each is the part that must not change.

Once nothing on the Keyup host opens a device node, any device passthrough into it is dead
weight and can be removed. That is a one-way door for the single-box fallback (§3.3 and the
`stt-ovms` profile need the nodes passed through again), so do it deliberately.

## 4. Teaching it your system's words

Whatever the backend, whisper does better when it is told what it is likely to hear. Four
settings carry that, all blank on a new install. The first three are runtime settings
(Admin → Settings → Transcription; a value set there wins over `.env`), the fourth is read
from `.env` at start-up.

| `.env` | Admin → Settings | What it does |
|---|---|---|
| `STT_PLACES` | *Place names* | Comma-separated towns, campuses and landmarks heard on the system, e.g. `STT_PLACES=Springfield,Shelbyville,State University`. They join the domain prompt and the hotword list after the talkgroup and unit aliases, so the engine spells them the local way. |
| `STT_PROMPT_EXTRA` | *Extra prompt* | Free text appended to the domain prompt Keyup builds from the talkgroup aliases, units and places — agency names, callsigns, street names. |
| `STT_HALLUCINATION_PATTERNS` | *Hallucination patterns* | Extra regular expressions, one per line, that mark a transcript as a hallucination (labeled `noise`, no text kept), merged with the built-in caption-credit and sign-off patterns. |
| `STT_TEXT_REWRITES` | — | Repairs applied to every transcript before it is judged: `pattern => replacement` rules, `;`-separated (or one per line), case-insensitive. For words whisper writes as something else — typically a short callsign or place name that reads like a brand and comes back as a web domain: `STT_TEXT_REWRITES='\bxcom(\.com)+(\.au)?\b => XCOM'`. Quote the value in `.env`; a rule without `=>` or with an invalid pattern is logged and skipped. |

The domain prompt is capped at 800 characters and filled in order of value (the talkgroup
aliases first, places last), so a long place list cannot crowd out the channel names. The
OVMS backends (§3.9) accept no prompt and no hotwords, so the first two do nothing there;
rewrites and hallucination patterns apply to every backend, because they run on the text
Keyup gets back (`app/stt/postprocess.py`, `app/stt/prompt.py`).

To see the effect before and after a change, run the same calls through
`python -m app.stt.bench` (§1).
