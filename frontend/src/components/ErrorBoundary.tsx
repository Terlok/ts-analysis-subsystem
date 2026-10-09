import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  resetKey?: string; // a change (e.g. other channels) clears the error
}

/** Keeps a rendering error in one part of the page (a chart) from blanking the whole UI. */
export class ErrorBoundary extends Component<Props, { error: Error | null; key?: string }> {
  state: { error: Error | null; key?: string } = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: { error: Error | null; key?: string }) {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error) {
    console.error("chart error", error);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="placeholder">
        Не вдалося намалювати графік: {this.state.error.message}{" "}
        <button className="btn" onClick={() => this.setState({ error: null })}>
          Перемалювати
        </button>
      </div>
    );
  }
}
