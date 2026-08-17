"use client";

import { useState, useRef, useEffect } from "react";

export default function Home() {
  const [messages, setMessages] = useState<{ role: string; content: string }[]>([
    { role: "assistant", content: "Good day. I am the Ejentic AI Executive RAG System. How may I assist you with your business needs today?" }
  ]);
  const [input, setInput] = useState("");
  const [clearance, setClearance] = useState("guest");
  const [isLoading, setIsLoading] = useState(false);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleFileUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;

    setIsUploading(true);
    setUploadStatus("Uploading document...");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const res = await fetch("http://localhost:8015/upload", {
        method: "POST",
        body: formData,
      });

      if (!res.ok) throw new Error("Upload failed");
      
      const data = await res.json();
      setUploadStatus("Document indexed successfully.");
      setTimeout(() => setUploadStatus(null), 5000);
      
      // Notify the user in the chat
      setMessages((prev) => [
        ...prev, 
        { role: "assistant", content: `I have successfully parsed and indexed the document: ${file.name}. You may now ask me questions about its contents.` }
      ]);
    } catch (error) {
      console.error(error);
      setUploadStatus("Failed to index document.");
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim()) return;

    const userMessage = input.trim();
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: userMessage }]);
    setIsLoading(true);

    try {
      // Connect to the backend on port 8015
      const res = await fetch("http://localhost:8015/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: userMessage, clearance_level: clearance }),
      });

      if (!res.ok) {
        throw new Error("Network response was not ok");
      }

      // Add a placeholder message for the assistant
      setMessages((prev) => [...prev, { role: "assistant", content: "" }]);
      
      const reader = res.body?.getReader();
      const decoder = new TextDecoder();
      
      if (!reader) return;
      
      let assistantMessage = "";
      let buffer = "";
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n\n");
        buffer = lines.pop() || "";
        
        for (const line of lines) {
          if (line.startsWith("data: ")) {
            const data = line.slice(6);
            if (data === "[DONE]") {
              break;
            }
            try {
              const parsed = JSON.parse(data);
              if (parsed.chunk) {
                assistantMessage += parsed.chunk;
                setMessages((prev) => {
                  const newMessages = [...prev];
                  newMessages[newMessages.length - 1].content = assistantMessage;
                  return newMessages;
                });
              } else if (parsed.error) {
                console.error("Backend error:", parsed.error);
                assistantMessage += "\n\n[Error: " + parsed.error + "]";
                setMessages((prev) => {
                  const newMessages = [...prev];
                  newMessages[newMessages.length - 1].content = assistantMessage;
                  return newMessages;
                });
              }
            } catch (e) {
              console.error("Error parsing SSE data", e, data);
            }
          }
        }
      }
    } catch (error) {
      console.error("Error fetching chat response:", error);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "System error: Unable to connect to the Ejentic Knowledge Core. Please verify server status." },
      ]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <main className="flex min-h-screen flex-col items-center justify-between p-4 md:p-12 bg-neutral-900 text-neutral-100 font-sans selection:bg-neutral-700">
      
      {/* Header */}
      <div className="w-full max-w-5xl flex items-center justify-between mb-8 pb-6 border-b border-neutral-800">
        <div className="flex items-center space-x-4">
          <div className="flex items-center justify-center w-10 h-10 bg-white text-black font-bold text-xl rounded-md shadow-md">
            E
          </div>
          <div>
            <h1 className="text-2xl font-semibold text-white tracking-tight">Ejentic AI</h1>
            <p className="text-sm text-neutral-400">Enterprise Hybrid RAG System</p>
          </div>
        </div>
        
        {/* Upload Button */}
        <div className="flex items-center space-x-3">
          {uploadStatus && (
            <span className="text-sm text-emerald-400 flex items-center space-x-1">
              <span>✓</span>
              <span>{uploadStatus}</span>
            </span>
          )}
          <select 
            value={clearance}
            onChange={(e) => setClearance(e.target.value)}
            className="bg-neutral-800 text-neutral-200 text-sm font-medium py-2 px-3 rounded-md border border-neutral-700 outline-none hover:bg-neutral-700 transition-colors cursor-pointer"
          >
            <option value="guest">Guest Clearance</option>
            <option value="employee">Employee Clearance</option>
            <option value="executive">Executive Clearance</option>
          </select>
          <input 
            type="file" 
            accept=".pdf" 
            className="hidden" 
            ref={fileInputRef} 
            onChange={handleFileUpload} 
          />
          <button 
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading}
            className="flex items-center space-x-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-200 text-sm font-medium py-2 px-4 rounded-md border border-neutral-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isUploading ? <span className="animate-spin text-lg leading-none">↻</span> : <span>↑</span>}
            <span>Upload Document</span>
          </button>
        </div>
      </div>

      {/* Chat Container */}
      <div className="flex flex-col w-full max-w-5xl h-[75vh] bg-neutral-950 border border-neutral-800 rounded-xl shadow-2xl overflow-hidden relative">
        
        {/* Chat Messages */}
        <div className="flex-1 overflow-y-auto p-6 md:p-8 space-y-6 scroll-smooth">
          {messages.map((m, index) => (
            <div
              key={index}
              className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[85%] rounded-lg p-5 leading-relaxed shadow-sm ${
                  m.role === "user"
                    ? "bg-neutral-800 text-white border border-neutral-700"
                    : "bg-neutral-900 text-neutral-200 border border-neutral-800"
                }`}
              >
                <p className="whitespace-pre-wrap">{m.content}</p>
              </div>
            </div>
          ))}
          {isLoading && (
            <div className="flex justify-start">
              <div className="max-w-[80%] rounded-lg p-5 bg-neutral-900 border border-neutral-800 text-neutral-400 flex items-center space-x-3">
                <span className="animate-spin text-lg leading-none">↻</span>
                <span className="text-sm font-medium">Processing query...</span>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div className="p-4 bg-neutral-900 border-t border-neutral-800">
          <form onSubmit={handleSubmit} className="flex space-x-3">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Query the Ejentic Knowledge Base or an uploaded document..."
              className="flex-1 bg-neutral-950 border border-neutral-800 text-white rounded-lg px-5 py-4 focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600 text-sm transition-all"
              disabled={isLoading}
            />
            <button
              type="submit"
              disabled={isLoading || !input.trim()}
              className="flex items-center justify-center bg-white hover:bg-neutral-200 text-black font-semibold py-4 px-8 rounded-lg transition-all disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <span className="mr-2">➤</span>
              Send
            </button>
          </form>
        </div>
      </div>
    </main>
  );
}
