import { LogIn } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useNavigate } from "react-router";
import { isAuthError } from "../api/client";
import { useCurrentUser, useLogin } from "../api/hooks";
import { Button } from "../components/ui/Button";
import { Card } from "../components/ui/Card";
import { Field, TextInput } from "../components/ui/Field";
import { Tip } from "../components/ui/Tooltip";
import { HELP, HelpText } from "../help/help";
import { useDocumentTitle } from "../lib/useDocumentTitle";
import { useSignInStore } from "../state/auth";

/**
 * Sign in. Reading the archive never needs an account — this page exists only so
 * that changing something can.
 */
export function Login() {
  const navigate = useNavigate();
  const login = useLogin();
  const { user } = useCurrentUser();
  const redirectTo = useSignInStore((state) => state.redirectTo);
  const setRedirect = useSignInStore((state) => state.setRedirect);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  useDocumentTitle("Sign in");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate(
      { username, password },
      {
        onSuccess: () => {
          const destination = redirectTo ?? "/calls";
          setRedirect(null);
          void navigate(destination);
        },
      },
    );
  };

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        {/* The page's one heading. `CardHeader` renders an h2, which left sign-in with none. */}
        <header className="border-b border-line px-4 py-3">
          <h1 className="text-sm font-semibold text-ink">Sign in</h1>
          <p className="mt-0.5 text-xs text-ink-2">
            <HelpText id="login.why" />
          </p>
        </header>
        {user ? (
          <div className="space-y-3 p-4">
            <p className="text-sm text-ink">
              You are signed in as <span className="font-medium">{user.username}</span>.
            </p>
            <Button
              tip="Go back to the calls"
              variant="primary"
              className="w-full"
              onClick={() => void navigate("/calls")}
            >
              Continue
            </Button>
          </div>
        ) : (
          <form onSubmit={submit} className="space-y-3 p-4">
            <Field label="Username" help="login.username">
              {(id) => (
                <TextInput
                  id={id}
                  value={username}
                  autoComplete="username"
                  required
                  onChange={(event) => setUsername(event.target.value)}
                />
              )}
            </Field>
            <Field label="Password" help="login.password">
              {(id) => (
                <TextInput
                  id={id}
                  type="password"
                  value={password}
                  autoComplete="current-password"
                  required
                  onChange={(event) => setPassword(event.target.value)}
                />
              )}
            </Field>

            {login.error ? (
              <p role="alert" className="text-xs text-critical-ink">
                {isAuthError(login.error)
                  ? "That username and password do not match an account."
                  : "Could not reach the sign-in service. Try again in a moment."}
              </p>
            ) : null}

            <Tip label={HELP["login.submit"]}>
              <button
                type="submit"
                disabled={login.isPending}
                className="inline-flex h-9 w-full items-center justify-center gap-1.5 rounded-md bg-accent-fill px-3 text-sm font-medium text-white hover:bg-accent-hover disabled:opacity-50"
              >
                <LogIn size={15} aria-hidden="true" />
                {login.isPending ? "Signing in…" : "Sign in"}
              </button>
            </Tip>
          </form>
        )}
      </Card>
    </div>
  );
}
