import { AlertTriangle, KeyRound, Lock, RefreshCw, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import {
  useApplyHttps,
  useCurrentUser,
  useHttpsStatus,
  useSaveSettings,
  useSettings,
  useTestCloudflareToken,
} from "../../api/hooks";
import { HTTPS_SETTING_KEYS, type HttpsDnsTestResult, type HttpsStatus } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { Card, CardHeader } from "../../components/ui/Card";
import { StatusChip } from "../../components/ui/Chip";
import { SkeletonText } from "../../components/ui/Skeleton";
import { ErrorState } from "../../components/ui/States";
import { HELP } from "../../help/help";
import { formatRelative } from "../../lib/format";
import { mutationErrorToast, toast } from "../../state/toasts";
import { SettingField } from "./SettingField";

/** The settings group this tab owns. */
const GROUP = "HTTPS";

/**
 * Admin → HTTPS: the archive's own certificate, configured without a redeploy.
 *
 * This is possible because Caddy takes a whole new configuration over its admin API and
 * swaps it in atomically, so a save here re-renders the proxy configuration and loads it
 * into the running container. The order on the page follows what an operator actually
 * does: read what is being served now, fill the form in, prove the token, save.
 *
 * The warning box is not decoration. The failure mode worth knowing about is quiet: a
 * wrong token means no certificate, and the site stays on plain HTTP rather than breaking
 * — which looks like nothing happened.
 */
export function HttpsTab() {
  const { isAdmin } = useCurrentUser();
  const status = useHttpsStatus(isAdmin);
  const settings = useSettings();
  const save = useSaveSettings();
  const apply = useApplyHttps();
  const testToken = useTestCloudflareToken();

  const [draft, setDraft] = useState<Record<string, string>>({});
  const [tokenTest, setTokenTest] = useState<HttpsDnsTestResult | null>(null);

  // The API's order within a group is the spec's order; this is the order the form reads
  // in — switch, names, contact, challenge, credential, authority, redirect.
  const items = useMemo(() => {
    const rows = (settings.data ?? []).filter((setting) => setting.group === GROUP);
    const rank = (key: string) => {
      const index = (HTTPS_SETTING_KEYS as readonly string[]).indexOf(key);
      return index === -1 ? HTTPS_SETTING_KEYS.length : index;
    };
    return [...rows].sort((a, b) => rank(a.key) - rank(b.key));
  }, [settings.data]);

  const dirty = Object.keys(draft).length > 0;

  if (!isAdmin) {
    return (
      <Card>
        <CardHeader title="HTTPS" hint={HELP["admin.tab.https"]} />
        <p className="p-4 text-sm text-ink-2">
          Only an administrator can see or change the TLS configuration — it holds a Cloudflare API token.
        </p>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <StatusCard status={status} onApply={apply} />

      <Card>
        <CardHeader
          title="Configuration"
          hint={HELP["admin.https.settings"]}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="ghost"
                tip={HELP["admin.https.testDns"]}
                disabled={testToken.isPending}
                onClick={() =>
                  testToken.mutate(undefined, {
                    onSuccess: setTokenTest,
                    onError: (error) => mutationErrorToast(error, "Could not reach Cloudflare"),
                  })
                }
              >
                <KeyRound size={13} aria-hidden="true" /> Test Cloudflare token
              </Button>
              <Button
                size="sm"
                variant="primary"
                tip="Save these settings and load them into the running proxy"
                disabled={!dirty || save.isPending}
                onClick={() =>
                  save.mutate(draft, {
                    onSuccess: () => {
                      toast("HTTPS settings saved and pushed to the proxy", { tone: "success" });
                      setDraft({});
                      setTokenTest(null);
                      status.refetch();
                    },
                    onError: (error) => mutationErrorToast(error, "Could not save the settings"),
                  })
                }
              >
                Save changes
              </Button>
            </div>
          }
        />
        {settings.isLoading ? (
          <SkeletonText lines={6} className="p-4" />
        ) : (
          <div className="grid gap-3 p-4 sm:grid-cols-2">
            {items.map((setting) => (
              <SettingField
                key={setting.key}
                setting={setting}
                value={draft[setting.key] ?? setting.value}
                dirty={setting.key in draft}
                disabled={false}
                onChange={(next) => setDraft({ ...draft, [setting.key]: next })}
              />
            ))}
          </div>
        )}

        {tokenTest ? (
          <div
            className={`mx-4 mb-4 rounded border p-3 text-xs ${
              tokenTest.ok
                ? "border-success-line bg-success-bg text-success-ink"
                : "border-critical-line bg-critical-bg text-critical-ink"
            }`}
          >
            <p className="font-medium">
              {tokenTest.ok ? "Cloudflare accepted the token" : "Cloudflare refused the token"}
            </p>
            <p className="mt-1">{tokenTest.detail}</p>
            {tokenTest.zones.length > 0 ? (
              <ul className="mt-1 list-inside list-disc">
                {tokenTest.zones.map((zone) => (
                  <li key={zone.zone}>
                    {zone.zone} — {zone.found ? "visible to this token" : "NOT visible to this token"}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </Card>

      <Card>
        <CardHeader title="Before you turn it on" hint={HELP["admin.tab.https"]} />
        <div className="space-y-3 p-4 text-xs text-ink-2">
          <div className="flex gap-2 rounded border border-warning-line bg-warning-bg p-3 text-warning-ink">
            <AlertTriangle size={16} aria-hidden="true" className="mt-0.5 shrink-0" />
            <div className="space-y-1.5">
              <p className="font-medium">A failed challenge leaves the site on plain HTTP, quietly.</p>
              <p>
                With the <code className="font-mono">dns</code> challenge, a token Cloudflare refuses means
                DNS-01 cannot be answered and no certificate is issued; with{" "}
                <code className="font-mono">http</code>, the same happens when the names do not resolve
                publicly to this box or ports 80 and 443 are not reachable from the internet. The proxy does
                not fail: it keeps serving plain HTTP on port 80 and https simply never comes up, so the
                symptom is "nothing happened" rather than an error page. Press{" "}
                <strong>Test Cloudflare token</strong> before saving, and read the status card above
                afterwards — "wanted TLS, serving plain" is exactly this.
              </p>
              <p>
                The API is never redirected either way. Recorders push to{" "}
                <code className="font-mono">http://&lt;this box&gt;/api/call-upload</code> by IP address, and
                no certificate can cover an IP — so <code className="font-mono">/api/*</code> and{" "}
                <code className="font-mono">/health</code> keep answering over plain HTTP. Ingest does not
                break during a cutover, and it does not break during a failed one.
              </p>
            </div>
          </div>
          <p>
            <strong>A redeploy restarts the proxy.</strong> It comes back on whatever{" "}
            <code className="font-mono">.env</code> says, which is the bootstrap and not necessarily what is
            set here; the API pushes these settings again a second or two after it starts, and the worker
            re-checks every five minutes. So a deploy can show the old mode for a few seconds. Nothing is
            lost, and the certificate itself lives in a volume that survives deploys.
          </p>
          <p>
            <strong>Rehearse on staging.</strong> Let's Encrypt's production rate limits are weekly and
            unforgiving. Set the certificate authority to <code className="font-mono">staging</code>, confirm
            a certificate appears below (browsers will call it untrusted — that is the point), then switch to{" "}
            <code className="font-mono">production</code>.
          </p>
        </div>
      </Card>
    </div>
  );
}

function StatusCard({
  status,
  onApply,
}: {
  status: ReturnType<typeof useHttpsStatus>;
  onApply: ReturnType<typeof useApplyHttps>;
}) {
  const data = status.data;
  return (
    <Card>
      <CardHeader
        title="Status"
        hint={HELP["admin.https.status"]}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="ghost" tip="Re-read the proxy" onClick={() => status.refetch()}>
              <RefreshCw size={13} aria-hidden="true" /> Refresh
            </Button>
            <Button
              size="sm"
              variant="secondary"
              tip={HELP["admin.https.apply"]}
              disabled={onApply.isPending}
              onClick={() =>
                onApply.mutate(undefined, {
                  onSuccess: (result) =>
                    toast(
                      result.ok
                        ? `Proxy reloaded in ${result.mode} mode`
                        : `The proxy refused it: ${result.detail}`,
                      { tone: result.ok ? "success" : "error" },
                    ),
                  onError: (error) => mutationErrorToast(error, "Could not reach the proxy"),
                })
              }
            >
              <Lock size={13} aria-hidden="true" /> Apply now
            </Button>
          </div>
        }
      />
      {status.isLoading ? (
        <SkeletonText lines={3} className="p-4" />
      ) : status.error ? (
        <ErrorState error={status.error} onRetry={() => status.refetch()} />
      ) : data ? (
        <div className="space-y-3 p-4">
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-2">
            <StatusChip status={modeStatus(data)} tip={modeExplanation(data)} />
            <span>{modeExplanation(data)}</span>
          </div>

          {data.missing.length > 0 ? (
            <p className="rounded border border-warning-line bg-warning-bg p-2 text-xs text-warning-ink">
              HTTPS is switched on but still needs {data.missing.map(fieldName).join(", ")}. Until they are
              filled in the proxy serves plain HTTP.
            </p>
          ) : null}

          <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-4">
            <Field label="Wanted" value={data.desired_mode === "tls" ? "HTTPS" : "plain HTTP"} />
            <Field
              label="Serving"
              value={data.mode === "unknown" ? "unreachable" : data.mode === "tls" ? "HTTPS" : "plain HTTP"}
            />
            <Field label="Plain :80" value={data.redirect_http ? "redirects to https" : "serves the site"} />
            <Field label="Authority" value={data.acme_ca} />
            <Field label="Domains" value={data.domains.join(" ") || "—"} />
            <Field label="Proxy answers for" value={data.caddy_domains.join(" ") || "—"} />
            <Field label="ACME contact" value={data.acme_email || "—"} />
            <Field label="Cloudflare token" value={data.has_token ? "stored" : "not set"} />
          </dl>

          {data.certificates.length > 0 ? (
            // Scrolls sideways on a phone; nothing inside takes focus, so the box itself is
            // the tab stop a keyboard scrolls it from (and its focus ring is the global one).
            <section className="overflow-x-auto rounded" aria-label="Certificates" tabIndex={0}>
              <table className="w-full min-w-[36rem] text-xs">
                <thead>
                  <tr className="text-left text-xs text-muted">
                    <th className="py-1 pr-3 font-medium">Domain</th>
                    <th className="py-1 pr-3 font-medium">Issuer</th>
                    <th className="py-1 pr-3 font-medium">Expires</th>
                    <th className="py-1 pr-3 font-medium">Days left</th>
                  </tr>
                </thead>
                <tbody>
                  {data.certificates.map((certificate) => (
                    <tr key={certificate.domain} className="border-t border-line">
                      <td className="py-1 pr-3 font-mono">{certificate.domain}</td>
                      <td className="py-1 pr-3">
                        {certificate.ok ? (
                          <span className="inline-flex items-center gap-1">
                            {certificate.trusted ? (
                              <ShieldCheck size={12} aria-hidden="true" className="text-good-ink" />
                            ) : (
                              <AlertTriangle size={12} aria-hidden="true" className="text-warning-ink" />
                            )}
                            {certificate.issuer ?? "unknown"}
                            {certificate.trusted ? "" : " (not trusted — staging?)"}
                          </span>
                        ) : (
                          <span className="text-critical-ink">no certificate: {certificate.error}</span>
                        )}
                      </td>
                      <td className="py-1 pr-3 tabular-nums">
                        {certificate.not_after ? formatRelative(certificate.not_after) : "—"}
                      </td>
                      <td
                        className={`py-1 pr-3 tabular-nums ${
                          certificate.days_left !== null && certificate.days_left < 15
                            ? "text-warning-ink"
                            : ""
                        }`}
                      >
                        {certificate.days_left ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ) : null}

          <p className="text-[11px] text-muted">
            Last applied {data.last_applied_at ? formatRelative(data.last_applied_at) : "never"}.
            {data.last_error ? (
              <span className="text-critical-ink"> Last error: {data.last_error}</span>
            ) : null}
          </p>
        </div>
      ) : null}
    </Card>
  );
}

/** `ok` / `warn` / `error`, from the two things that can disagree. */
function modeStatus(data: HttpsStatus): string {
  if (!data.reachable) return "error";
  if (data.desired_mode === data.mode) return "ok";
  return "warn";
}

function modeExplanation(data: HttpsStatus): string {
  if (!data.reachable) {
    return `The proxy's admin API is not answering${data.error ? ` (${data.error})` : ""}. The site itself may well be fine; nothing here can be changed until it is back.`;
  }
  if (data.desired_mode === "tls" && data.mode === "tls") {
    return "Serving HTTPS for the hostnames below.";
  }
  if (data.desired_mode === "tls") {
    return "These settings ask for HTTPS but the proxy is serving plain HTTP — a certificate has not been issued, or the configuration has not been applied.";
  }
  if (data.mode === "tls") {
    return "The proxy is still serving HTTPS although these settings say plain HTTP. Press Apply now.";
  }
  return "Serving plain HTTP. This is the default, and the whole stack works this way.";
}

const FIELD_NAMES: Record<string, string> = {
  tls_domains: "the domains",
  tls_acme_email: "an ACME contact address",
  tls_cloudflare_api_token: "a Cloudflare API token",
};

function fieldName(key: string): string {
  return FIELD_NAMES[key] ?? key;
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="truncate text-ink" title={value}>
        {value}
      </dd>
    </div>
  );
}
