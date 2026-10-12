import { Composer } from './components/Composer';
import { ErrorBanner } from './components/ErrorBanner';
import { Header } from './components/Header';
import { MessageList } from './components/MessageList';
import { useBackendMode } from './hooks/useBackendMode';
import { useChat } from './hooks/useChat';

export default function App() {
  const chat = useChat();
  const healthMode = useBackendMode();

  return (
    <div className="app">
      <Header mode={chat.mode ?? healthMode} onNewConversation={chat.reset} />
      <MessageList
        messages={chat.messages}
        pending={chat.pending}
        resumed={chat.threadId !== null && chat.messages.length === 0}
      />
      {chat.error && (
        <ErrorBanner error={chat.error} disabled={chat.pending} onRetry={chat.retry} />
      )}
      <Composer pending={chat.pending} onSend={chat.send} />
    </div>
  );
}
