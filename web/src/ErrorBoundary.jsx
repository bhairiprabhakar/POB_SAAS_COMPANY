import { Component } from 'react';

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null, info: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    this.setState({ info });
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary]', error, info);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const msg = this.state.error?.message || String(this.state.error);
    const stack = this.state.error?.stack || '';
    return (
      <div className="auth-page">
        <div className="auth-card" style={{ maxWidth: 640 }}>
          <div className="auth-brand">
            <span className="brand-mark lg" style={{ background: 'var(--red)' }}>!</span>
            <h1>Something went wrong</h1>
            <p className="muted">The page hit an unexpected error. Your data is safe — reload to continue.</p>
          </div>
          <div className="error-box">
            <strong>Error</strong>
            <span>{msg}</span>
          </div>
          {stack && <pre className="muted" style={{ fontSize: 11, maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap' }}>{stack}</pre>}
          <button className="btn btn-primary btn-block" style={{ marginTop: 12 }}
            onClick={() => window.location.reload()}>Reload page</button>
        </div>
      </div>
    );
  }
}