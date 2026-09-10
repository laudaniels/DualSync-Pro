import { useState } from 'react'
import DualMixer from './components/DualMixer'
import './styles/App.css'

export default function App() {
  return (
    <div className="app">
      <header className="app-header">
        <div className="header-content">
          <h1>🎵 DualSync Pro</h1>
          <p className="tagline">Real-time dual-track mixing with AI stem separation</p>
        </div>
      </header>

      <main className="app-main">
        <DualMixer />
      </main>

      <footer className="app-footer">
        <p>Load up to 2 songs → AI separates into stems → Mix & create mashups in real-time</p>
      </footer>
    </div>
  )
}
