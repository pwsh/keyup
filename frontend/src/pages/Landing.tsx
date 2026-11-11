import { Link, Navigate } from "react-router";
import { useCurrentUser } from "../api/hooks";
import { PageHeader } from "../components/layout/PageHeader";
import { isPublicHost, publicSite } from "../lib/publicSite";

const LINK = "text-accent-ink hover:underline";

/** The public host's front page; everywhere else, and for a signed-in reader, the calls. */
export function Home() {
  const { user, isLoading } = useCurrentUser();
  if (!isPublicHost() || (!isLoading && user !== null)) return <Navigate to="/calls" replace />;
  return <Landing />;
}

function Landing() {
  const { name, description } = publicSite();
  return (
    <div className="p-6">
      <PageHeader title={name} description={description} />
      <div className="mt-4 max-w-2xl space-y-4">
        <p>
          Calls on the county's trunked radio system are recorded here as they happen, labelled by talkgroup
          and transcribed by software.
        </p>
        <ul className="list-disc space-y-1 pl-5">
          <li>
            <Link className={LINK} to="/live">
              Listen live
            </Link>
          </li>
          <li>
            <Link className={LINK} to="/calls">
              Browse recent calls
            </Link>
          </li>
          <li>
            <Link className={LINK} to="/talkgroups">
              Talkgroups
            </Link>
          </li>
          <li>
            <Link className={LINK} to="/stats">
              Statistics
            </Link>
          </li>
          <li>
            <Link className={LINK} to="/help">
              Help
            </Link>
          </li>
        </ul>
        <p>
          An independent site, not affiliated with any government agency. Do not rely on it in an emergency:
          call 911.
        </p>
        <p>
          <Link className={LINK} to="/privacy">
            Privacy and removal requests
          </Link>
        </p>
      </div>
    </div>
  );
}
