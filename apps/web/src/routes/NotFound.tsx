import { Link } from 'react-router-dom'

export function NotFound() {
  return (
    <div className="panel">
      <h2>Not found</h2>
      <p className="muted">
        That page does not exist. <Link to="/">Back to the library</Link>.
      </p>
    </div>
  )
}
