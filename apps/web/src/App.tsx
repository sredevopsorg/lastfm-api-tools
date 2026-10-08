import { NavLink, Route, Routes } from 'react-router-dom'
import { Library } from './routes/Library'
import { Editor } from './routes/Editor'
import { Bulk } from './routes/Bulk'
import { Review } from './routes/Review'
import { Archive } from './routes/Archive'
import { GenreRemoval } from './routes/GenreRemoval'
import { Settings } from './routes/Settings'
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
          <NavLink to="/remove-genre">Remove genre</NavLink>
          <NavLink to="/archive">Archive</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
      </header>
      <Routes>
        <Route path="/" element={<Library />} />
        <Route path="/edit/:itemId" element={<Editor />} />
        <Route path="/review" element={<Review />} />
        <Route path="/bulk" element={<Bulk />} />
        <Route path="/remove-genre" element={<GenreRemoval />} />
        <Route path="/archive" element={<Archive />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </div>
  )
}
