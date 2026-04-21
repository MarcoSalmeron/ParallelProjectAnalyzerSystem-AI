import { useState, useEffect, useRef, useCallback } from 'react';
import { createAnalysis, getWebSocketUrl } from '../api/apiConfig';

export const AGENTS = [
  { id: 1, name: 'Supervisor', icon: 'S', color: 'bg-oracle-accent' },
  { id: 2, name: 'Investigador', icon: 'I', color: 'bg-oracle-success' },
  { id: 3, name: 'Analista', icon: 'A', color: 'bg-oracle-warning' },
  { id: 4, name: 'Redactor', icon: 'R', color: 'bg-oracle-primary' },
];

export const useOracleWorkflow = () => {
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [currentStep, setCurrentStep] = useState(0);
  const [agentStatuses, setAgentStatuses] = useState(
    AGENTS.map(agent => ({ ...agent, status: 'waiting', log: '' }))
  );
  const [messages, setMessages] = useState([]);
  const [pdfUrl, setPdfUrl] = useState(null);
  const [error, setError] = useState(null);
  const wsRef = useRef(null);
  const threadIdRef = useRef(null);

  // 👉 función única para reanudar
  async function resumeAnalysis(respuesta) {
    if (!threadIdRef.current) return;
    await fetch(`/impact/resume/${threadIdRef.current}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ erp_module: respuesta })
    });
  }

  const connectWebSocket = useCallback((threadId) => {
    const wsUrl = getWebSocketUrl(threadId);
    wsRef.current = new WebSocket(wsUrl);

    wsRef.current.onopen = () => {
      console.log('WebSocket connected');
    };

    wsRef.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        handleWebSocketMessage(data);
      } catch (err) {
        console.error('Error parsing WebSocket message:', err);
      }
    };

    wsRef.current.onerror = (err) => {
      console.error('WebSocket error:', err);
      setError('Error de conexión');
    };

    wsRef.current.onclose = () => {
      console.log('WebSocket disconnected');
    };
  }, []);

  const handleWebSocketMessage = useCallback((data) => {
    if (data.error) {
      setError(data.error);
      setIsAnalyzing(false);
      return;
    }

    // 👉 caso de interrupción
 if (data.type === "interrupt") {
  setMessages(prev => [...prev, {
    id: Date.now(),
    agent: "system",
    type: "interrupt",   // Human in the Loop
    content: data.content,
    timestamp: new Date().toISOString(),
  }]);
  return;
}

    const { step, agent, status, content, log, pdf_ready, pdf_url } = data;

    if (step) {
      setCurrentStep(step);
      setAgentStatuses(prev =>
        prev.map(a => {
          if (a.id === step) {
            return { ...a, status: status || 'active', log: log || '' };
          }
          if (a.id < step) {
            return { ...a, status: 'completed', log: 'Completado' };
          }
          return a;
        })
      );
    }

    if (content) {
      setMessages(prev => [...prev, {
        id: Date.now(),
        agent: agent || 'system',
        content,
        timestamp: new Date().toISOString(),
      }]);
    }

    if (pdf_ready && pdf_url) {
      setPdfUrl(pdf_url);
      setIsAnalyzing(false);
      setAgentStatuses(prev =>
        prev.map(a => ({ ...a, status: 'completed', log: 'Completado' }))
      );
    }
  }, []);

  const startAnalysis = useCallback(async (query) => {
    setIsAnalyzing(true);
    setError(null);
    setPdfUrl(null);
    setCurrentStep(0);
    setAgentStatuses(AGENTS.map(agent => ({ ...agent, status: 'waiting', log: '' })));

    setMessages(prev => [
      ...prev,
      { id: Date.now(), agent: 'user', content: query, timestamp: new Date().toISOString() },
      { id: Date.now() + 1, agent: 'system', content: 'Iniciando análisis...', timestamp: new Date().toISOString() }
    ]);

    try {
      const response = await createAnalysis(query);
      const { thread_id } = response;
      threadIdRef.current = thread_id;
      connectWebSocket(thread_id);

      setMessages(prev => [...prev, {
        id: Date.now() + 2,
        agent: 'system',
        content: `Análisis iniciado. Thread ID: ${thread_id}`,
        timestamp: new Date().toISOString(),
      }]);
    } catch (err) {
      setError(err.response?.data?.detail || 'Error al iniciar el análisis');
      setIsAnalyzing(false);
    }
  }, [connectWebSocket]);

  const resetWorkflow = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
    }
    setIsAnalyzing(false);
    setCurrentStep(0);
    setAgentStatuses(AGENTS.map(agent => ({ ...agent, status: 'waiting', log: '' })));
    setMessages([]);
    setPdfUrl(null);
    setError(null);
    threadIdRef.current = null;
  }, []);

  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
      }
    };
  }, []);

  return {
    isAnalyzing,
    currentStep,
    agentStatuses,
    messages,
    pdfUrl,
    error,
    startAnalysis,
    resetWorkflow,
    resumeAnalysis, // 👉 expuesto para que ChatBox lo use
  };
};
