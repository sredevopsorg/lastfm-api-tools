import { NavLink, Route, Routes } from 'react-router-dom'
import { Library } from './routes/Library'
import { Editor } from './routes/Editor'
import { Bulk } from './routes/Bulk'
import { Archive } from './routes/Archive'
import { NotFound } from './routes/NotFound'

export function App() {
  return (
    <div className="app">
      <header className="app-header">
        <h1>metaedit</h1>
        <span className="tagline">Jellyfin metadata from Last.fm</span>
        <nav className="app-nav">
          <NavLink to="/" end>
            Library
          </NavLink>
          <NavLink to="/bulk">Bulk</NavLink>
          <NavLink to="/archive">Archive</NavLink>
        </nav>
      </header>
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/edit/:itemId" element={<Editor />} />
        <Route path="/bulk" element={<Bulk />} />
        <Route path="/archive" element={<Archive />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </div>
  )
}
