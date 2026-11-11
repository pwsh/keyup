"""Plain-English descriptions of the directory vocabularies, in one place.

:mod:`app.models` holds the *lists* — :data:`~app.models.TALKGROUP_CATEGORIES`,
:data:`~app.models.RR_SERVICE_TAGS`, :data:`~app.models.AGENCY_KINDS` and the
:data:`~app.models.SERVICE_TAG_TO_CATEGORY` mapping between two of them — because those
are what the database, the importers and the validators compare against.  What none of
them holds is *what a value means*, which is the only thing an operator staring at a
twenty-item dropdown actually needs.

Those sentences live here rather than in the help registry or in a Markdown file because
there are three consumers and they must not drift apart: the Help page renders them, the
docs page (``docs/TALKGROUPS.md``) is checked against them by a test, and the API serves
them at ``GET /api/directory/vocabulary`` so the frontend never has to keep its own copy
of a list the backend validates.  Adding a category to ``models`` and forgetting to say
what it is for is a test failure, not a silently blank cell.
"""

from __future__ import annotations

from typing import Any

from app.models import (
    AGENCY_KINDS,
    RR_SERVICE_TAGS,
    SERVICE_TAG_TO_CATEGORY,
    TALKGROUP_CATEGORIES,
)

# --------------------------------------------------------------------------------------
# categories — this archive's own coarse bucket
# --------------------------------------------------------------------------------------

#: The short label the UI puts on a category, where the stored key is an abbreviation
#: nobody says out loud.  Anything absent is capitalized from the key.
CATEGORY_LABELS: dict[str, str] = {
    "law": "Law enforcement",
    "fire": "Fire",
    "ems": "EMS",
    "multi": "Multi-service",
    "interop": "Interoperability",
    "pw": "Public works",
    "utility": "Utilities",
    "transit": "Transit",
    "rail": "Rail",
    "aviation": "Aviation",
    "schools": "Schools",
    "corrections": "Corrections",
    "security": "Security",
    "federal": "Federal",
    "military": "Military",
    "media": "Media",
    "business": "Business",
    "ham": "Amateur radio",
    "data": "Data",
    "other": "Other",
}

#: What belongs in each category, in the words the Help page and the docs both use.
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "law": (
        "Police, sheriff and campus-police traffic — dispatch, car-to-car and tactical "
        "channels alike. The busiest bucket in this archive and the one the Calls filter "
        "is reached for most."
    ),
    "fire": (
        "Fire-department channels: the dispatch channel an alarm goes out on and the "
        "fireground and talk channels the crews work on afterwards."
    ),
    "ems": (
        "Ambulance and paramedic traffic, and the hospital channels the crews reach the "
        "receiving end on — it is the same incident from the other side of the radio."
    ),
    "multi": (
        "Channels several services share as a matter of course: a combined dispatch "
        "channel, a joint tac channel, an EOC channel that is by definition everybody at "
        "once. Not the same as interop, which is for the channels set aside for it."
    ),
    "interop": (
        "The mutual-aid and interoperability channels that exist so agencies who do not "
        "normally share a system can talk on a bad day — the national and statewide "
        "calling channels and the county's own shared talkgroups."
    ),
    "pw": (
        "Public works: streets, sanitation, parks, snow and the county's own maintenance "
        "crews. Quiet most of the year and the busiest channel on the system in a storm."
    ),
    "utility": (
        "Water, sewer, gas and electric utilities — the crews who turn up when a line is "
        "down. Often a separate system this archive only hears the conventional half of."
    ),
    "transit": (
        "Buses and the paratransit fleets, plus the road-and-highway operations traffic "
        "that is not public works. Rail and aviation get their own buckets."
    ),
    "rail": (
        "Railroad road, yard and dispatcher channels, including the freight lines that cross the county."
    ),
    "aviation": (
        "Airport ground, tower and air-ambulance traffic, and the aircraft channels the "
        "county's own air support uses."
    ),
    "schools": (
        "School districts and universities: buses, buildings-and-grounds, athletics and "
        "the district's own security where it is not sworn law enforcement."
    ),
    "corrections": (
        "Jail and prison operations, transport and court security. Coloured with law "
        "because it is the same family of traffic, and kept separate because the volume "
        "and the retention question are not the same."
    ),
    "security": (
        "Private and institutional security that is not a sworn agency — hospital "
        "security, mall and campus patrols, event staff."
    ),
    "federal": (
        "Federal civilian agencies: the marshals, the alphabet agencies, the VA and the "
        "federal side of a joint operation."
    ),
    "military": (
        "Military channels, including the National Guard units that appear on a state "
        "system during a deployment or a disaster."
    ),
    "media": ("Broadcast and press channels — news crews, remote trucks and the outlets' own dispatch."),
    "business": (
        "Ordinary commercial licensees: taxi and tow companies, contractors, delivery "
        "fleets, the businesses that share a repeater."
    ),
    "ham": ("Amateur-radio channels, repeaters and the ARES/RACES nets that stand up during an event."),
    "data": (
        "Channels that carry data rather than voice — telemetry, paging, MDT and control "
        "traffic. Almost always worth turning transcription off on."
    ),
    "other": (
        "The bucket for a channel that belongs in none of the above, and where a channel "
        "nobody has classified ends up. A large `other` count on the Talkgroups page is a "
        "to-do list, not a category."
    ),
}

#: One sentence on what setting a category actually *changes* in the app.  It is the same
#: answer for all twenty, so it is said once rather than twenty times.
CATEGORY_EFFECT = (
    "A category colours the channel's icon on Calls and Live, gives the Calls filter rail "
    "and its facet counts something to group by, is the `category` an alert rule matches "
    "and a webhook payload carries, and is one of the columns the statistics page breaks "
    "volume down by. It does not affect recording, transcription or retention — those are "
    "the channel's own settings."
)

# --------------------------------------------------------------------------------------
# service tags — RadioReference's vocabulary, kept in RR's own words
# --------------------------------------------------------------------------------------

#: What RadioReference means by each of its service tags.  The wording is ours; the
#: vocabulary and its spelling are RR's, which is the point of keeping the column.
SERVICE_TAG_DESCRIPTIONS: dict[str, str] = {
    "Aircraft": (
        "Air-to-ground and airborne traffic — air ambulance, law-enforcement aircraft, airport operations."
    ),
    "Business": "A commercial licensee's own channel: taxis, tow trucks, contractors, delivery fleets.",
    "Corrections": "Jail, prison, inmate transport and court security.",
    "Data": "A channel carrying data rather than voice: telemetry, paging, MDT or control traffic.",
    "Deprecated": (
        "RR's marker for a channel that used to be something and no longer is; kept so a "
        "re-sync does not quietly resurrect it."
    ),
    "Emergency Ops": (
        "Emergency-management and EOC channels — the ones that come alive during a declared incident."
    ),
    "EMS Dispatch": "The channel an ambulance is dispatched on.",
    "EMS-Tac": "The tactical channel an EMS crew moves to once it is working an incident.",
    "EMS-Talk": "EMS administrative and general talk traffic that is neither dispatch nor tactical.",
    "Federal": "A federal civilian agency's channel.",
    "Fire Dispatch": "The channel a fire alarm goes out on.",
    "Fire-Tac": "A fireground or tactical channel the crews work an incident on.",
    "Fire-Talk": "Fire administrative and general talk traffic away from an incident.",
    "Ham": "Amateur radio: repeaters, nets and the ARES/RACES traffic that stands up in an emergency.",
    "Hospital": (
        "Hospital channels, including the med-control traffic between a crew and the receiving facility."
    ),
    "Interop": ("A mutual-aid or interoperability channel set aside for agencies who do not share a system."),
    "Law Dispatch": "The channel a police or sheriff unit is dispatched on.",
    "Law Tac": "A law-enforcement tactical channel: a pursuit, a perimeter, a warrant service.",
    "Law Talk": "Law-enforcement car-to-car and administrative traffic that is not dispatch or tactical.",
    "Media": "Broadcast and press: news crews, remote trucks and the outlets' own dispatch.",
    "Military": "Military channels, including National Guard units operating on a state system.",
    "Multi-Dispatch": (
        "One dispatch channel serving more than one service — a combined law/fire/EMS dispatch."
    ),
    "Multi-Tac": "A tactical channel more than one service works an incident on together.",
    "Multi-Talk": "General talk traffic shared by more than one service.",
    "Other": "RR's catch-all for a channel none of its other tags fits.",
    "Public Works": "Streets, sanitation, parks, snow removal and municipal maintenance crews.",
    "Railroad": "Railroad road, yard and dispatcher channels.",
    "Schools": "School districts and universities: buses, buildings and grounds, athletics.",
    "Security": "Private or institutional security that is not a sworn agency.",
    "Transportation": "Buses, paratransit and road-and-highway operations.",
    "Utilities": "Water, sewer, gas and electric utilities and their field crews.",
}

# --------------------------------------------------------------------------------------
# agencies
# --------------------------------------------------------------------------------------

#: What each agency kind names.  Six words, kept deliberately small so an agency list
#: exported from a CAD system maps onto them without a translation table.
AGENCY_KIND_DESCRIPTIONS: dict[str, str] = {
    "dispatch": (
        "The dispatch centre itself. Usually the one row of this kind, and it is what the "
        "shared and interop channels are mapped to when no single department owns them."
    ),
    "police": "A municipal police department.",
    "sheriff": "A county sheriff's office, including its jail and civil divisions.",
    "university": "A university's own department — campus police, campus fire, campus EMS.",
    "fire": "A fire department or fire district, volunteer or career.",
    "ems": "An ambulance service, whether county-run, hospital-based or private.",
}

#: Where an agency row came from.  ``seed`` rows are loaded from the deployment's agency CSV
#: and refreshed by ``keyup seed-agencies``; ``local`` rows were added in this app for an
#: agency that list has no code for, and the seed never touches them.
AGENCY_SOURCE_DESCRIPTIONS: dict[str, str] = {
    "seed": (
        "Loaded from the agency list this deployment seeds from (keyup seed-agencies). "
        "Keep the codes the same as an outside system's — a CAD system's agency codes, "
        "say — and a record there matches on them. Re-seeding refreshes the name unless "
        "somebody here has edited it."
    ),
    "local": (
        "Added in Keyup for an agency the seeded list has no code for — a neighbouring "
        "county, a railroad, a school district, a federal unit. It is yours to rename, "
        "deactivate and delete, and a re-seed never touches it."
    ),
}


def category_label(key: str) -> str:
    """The UI's label for a category key, capitalized from the key when unlisted."""
    return CATEGORY_LABELS.get(key, key.replace("_", " ").capitalize())


def categories() -> list[dict[str, Any]]:
    """Every talkgroup category, in the order :mod:`app.models` lists them."""
    return [
        {
            "key": key,
            "label": category_label(key),
            "description": CATEGORY_DESCRIPTIONS[key],
            # The RR tags that land a channel here, so the two tables read as one.
            "service_tags": [t for t in RR_SERVICE_TAGS if SERVICE_TAG_TO_CATEGORY.get(t) == key],
        }
        for key in TALKGROUP_CATEGORIES
    ]


def service_tags() -> list[dict[str, Any]]:
    """Every RadioReference service tag with its meaning and the category it maps to."""
    return [
        {
            "tag": tag,
            "category": SERVICE_TAG_TO_CATEGORY[tag],
            "category_label": category_label(SERVICE_TAG_TO_CATEGORY[tag]),
            "description": SERVICE_TAG_DESCRIPTIONS[tag],
        }
        for tag in RR_SERVICE_TAGS
    ]


def agency_kinds() -> list[dict[str, Any]]:
    """Every agency kind with what it names."""
    return [
        {"key": kind, "label": kind.capitalize(), "description": AGENCY_KIND_DESCRIPTIONS[kind]}
        for kind in AGENCY_KINDS
    ]


def agency_sources() -> list[dict[str, Any]]:
    """The two provenances an agency row can have."""
    return [
        {
            "key": key,
            "label": "Seeded" if key == "seed" else "Local",
            "description": text,
        }
        for key, text in AGENCY_SOURCE_DESCRIPTIONS.items()
    ]


def vocabulary() -> dict[str, Any]:
    """The whole payload ``GET /api/directory/vocabulary`` serves."""
    return {
        "categories": categories(),
        "category_effect": CATEGORY_EFFECT,
        "service_tags": service_tags(),
        "agency_kinds": agency_kinds(),
        "agency_sources": agency_sources(),
    }


# --------------------------------------------------------------------------------------
# the same thing as Markdown, for docs/TALKGROUPS.md
# --------------------------------------------------------------------------------------

#: The HTML-comment fences the generated tables sit between in ``docs/TALKGROUPS.md``.
#: A test regenerates each block and compares, so a category added to
#: :data:`~app.models.TALKGROUP_CATEGORIES` without a sentence here — or a docs table
#: edited by hand — fails rather than drifting quietly.
DOC_BLOCKS = ("categories", "service-tags", "agency-kinds", "agency-sources")


def _cell(text: str) -> str:
    """One table cell: no pipes, no newlines, or the row stops being a row."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def markdown_block(name: str) -> str:
    """The generated Markdown table called ``name``, without its fences."""
    if name == "categories":
        lines = [
            "| Category | Name | What belongs in it | RadioReference tags that land here |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{c['key']}` | {_cell(c['label'])} | {_cell(c['description'])} | "
            + (", ".join(f"`{t}`" for t in c["service_tags"]) or "—")
            + " |"
            for c in categories()
        ]
        return "\n".join(lines)
    if name == "service-tags":
        lines = [
            "| RadioReference tag | What RR means by it | Category it maps to |",
            "| --- | --- | --- |",
        ]
        lines += [f"| `{t['tag']}` | {_cell(t['description'])} | `{t['category']}` |" for t in service_tags()]
        return "\n".join(lines)
    if name == "agency-kinds":
        lines = ["| Kind | What it names |", "| --- | --- |"]
        lines += [f"| `{k['key']}` | {_cell(k['description'])} |" for k in agency_kinds()]
        return "\n".join(lines)
    if name == "agency-sources":
        lines = ["| `source` | Meaning |", "| --- | --- |"]
        lines += [f"| `{s['key']}` | {_cell(s['description'])} |" for s in agency_sources()]
        return "\n".join(lines)
    raise KeyError(name)


def fenced_block(name: str) -> str:
    """``markdown_block`` wrapped in the fences the docs file carries."""
    return f"<!-- generated:{name} -->\n{markdown_block(name)}\n<!-- /generated:{name} -->"


__all__ = [
    "AGENCY_KIND_DESCRIPTIONS",
    "AGENCY_SOURCE_DESCRIPTIONS",
    "CATEGORY_DESCRIPTIONS",
    "CATEGORY_EFFECT",
    "CATEGORY_LABELS",
    "DOC_BLOCKS",
    "SERVICE_TAG_DESCRIPTIONS",
    "agency_kinds",
    "agency_sources",
    "categories",
    "category_label",
    "fenced_block",
    "markdown_block",
    "service_tags",
    "vocabulary",
]
