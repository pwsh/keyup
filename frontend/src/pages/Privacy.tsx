import { PageHeader } from "../components/layout/PageHeader";
import { gtmContainerId } from "../lib/publicSite";

function Contact({ value }: { value: string }) {
  const href = value.includes("@") ? `mailto:${value}` : value;
  return (
    <a className="text-accent-ink hover:underline" href={href}>
      {value}
    </a>
  );
}

export function Privacy() {
  const contact = String(import.meta.env.VITE_PRIVACY_CONTACT ?? "").trim();
  const analytics = gtmContainerId() !== "";
  return (
    <div className="p-6">
      <PageHeader title="Privacy" description="What this site publishes and what it records about you." />
      <div className="mt-4 max-w-2xl space-y-4">
        <h2 className="text-lg font-semibold">What is published</h2>
        <p>Recordings and transcripts of radio calls are kept here and are searchable by anyone.</p>
        <p>
          Transcripts are made by software and contain mistakes, including wrong names and addresses. Check
          the recording before relying on one.
        </p>

        <h2 className="text-lg font-semibold">Removal requests</h2>
        <p>
          To ask for a call to be removed, send its link to the site's operator
          {contact ? (
            <>
              {" at "}
              <Contact value={contact} />
            </>
          ) : null}
          .
        </p>

        <h2 className="text-lg font-semibold">What is recorded about you</h2>
        <p>
          Reading this site needs no account and sets no cookie of its own. Signing in sets one session
          cookie, and your theme and player settings stay in your own browser.
        </p>
        {analytics ? (
          <p>
            To count visits, this site uses Google Analytics, loaded through Google Tag Manager. Google
            receives the kind of page you opened, your browser and approximate location, and sets its _ga
            cookies. It is not told which call you opened or what you searched for, and it is not used for
            signed-in readers. A content blocker turns it off without affecting the site.
          </p>
        ) : null}
      </div>
    </div>
  );
}
