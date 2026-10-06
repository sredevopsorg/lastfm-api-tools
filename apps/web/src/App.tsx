import { NavLink, Route, Routes } from 'react-router-dom'
import { Home } from './routes/Home'
import { Archive } from './routes/Archive'
import { NotFound } from './routes/NotFound'

export function App() {
  return (
    <div className="app">
      <header className="app-header">
        <h1>metaedit</h1>
        <span className="tagline">Jellyfin metadata from Last.fm</span>
        <nav className="app-nav">
          <NavLink to="/">Library</NavLink>
          <NavLink to="/archive">Archive</NavLink>
        </nav>
      </header>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/archive" element={<Archive />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </div>
  )
}
