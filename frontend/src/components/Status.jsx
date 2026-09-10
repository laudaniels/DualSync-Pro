export default function Status({ message }) {
  if (!message) return null

  const isSuccess = message.includes('✓') || message.includes('✨')
  const isError = message.includes('✗')

  return (
    <div className={`status ${isSuccess ? 'success' : isError ? 'error' : ''}`}>
      {message}
    </div>
  )
}
