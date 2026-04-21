import React, { useState, useRef, useEffect } from 'react';
import MessageItem from './MessageItem';

const ChatBox = ({ messages, onStartAnalysis, resumeAnalysis, isAnalyzing }) => {
  const [input, setInput] = useState('');
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

const handleSubmit = (e) => {
  e.preventDefault();
  if (!input.trim() || isAnalyzing) return;

  const query = input.trim();
  setInput('');

  const lastMsg = messages[messages.length - 1];
  // Interrupciones
  if (lastMsg && lastMsg.type === 'interrupt') {
    resumeAnalysis(query);
  } else {
    onStartAnalysis(query);
  }
};

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      handleSubmit(e);
    }
  };

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 border-b border-oracle-border">
        <h2 className="text-lg font-semibold text-oracle-text flex items-center gap-2">
          <svg className="w-5 h-5 text-oracle-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
          Chat
        </h2>
        <p className="text-xs text-oracle-muted mt-1">
          Consulta sobre Oracle Cloud ERP
        </p>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center">
            <div className="w-16 h-16 rounded-full bg-oracle-surface border border-oracle-border flex items-center justify-center mb-4">
              <svg className="w-8 h-8 text-oracle-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            </div>
            <h3 className="text-oracle-text font-medium">Ingeniería Condor Insights</h3>
            <p className="text-oracle-muted text-sm mt-1 max-w-xs">
              Describe tu consulta sobre Oracle Cloud ERP y nuestros agentes la analizarán
            </p>
          </div>
        ) : (
          messages.map((message) => (
            <MessageItem key={message.id} message={message} />
          ))
        )}
        <div ref={messagesEndRef} />
      </div>

      <form onSubmit={handleSubmit} className="p-4 border-t border-oracle-border">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Describe tu consulta sobre Oracle Cloud..."
            className="input-field flex-1"
            disabled={isAnalyzing}
          />
          <button
            type="submit"
            disabled={!input.trim() || isAnalyzing}
            className="btn-primary px-6"
          >
            {isAnalyzing ? (
              <svg className="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
              </svg>
            ) : (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
              </svg>
            )}
          </button>
        </div>
      </form>
    </div>
  );
};

export default ChatBox;