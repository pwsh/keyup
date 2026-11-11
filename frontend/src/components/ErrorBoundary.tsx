import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./ui/Button";
import { ErrorState } from "./ui/States";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/**
 * A render that throws below here would otherwise blank the whole app — a
 * malformed value in the URL is enough. The boundary keeps the chrome and offers
 * the two ways out.
 */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Unhandled render error", error, info.componentStack);
  }

  override render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="p-6">
        <ErrorState error={this.state.error} onRetry={() => this.setState({ error: null })} />
        <div className="mt-4 flex justify-center">
          <Button
            tip="Reload the whole page from scratch"
            onClick={() => window.location.assign("/calls")}
            size="sm"
          >
            Back to calls
          </Button>
        </div>
      </div>
    );
  }
}
