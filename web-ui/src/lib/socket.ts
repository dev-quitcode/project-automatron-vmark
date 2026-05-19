import { io, Socket } from "socket.io-client";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000";

let socket: Socket | null = null;

export function getSocket(): Socket {
  if (!socket) {
    socket = io(WS_URL, {
      transports: ["polling", "websocket"],
      autoConnect: false,
      reconnection: true,
      reconnectionAttempts: Infinity,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 10000,
    });
  }
  return socket;
}

export function connectSocket(): Socket {
  const s = getSocket();
  if (!s.connected) {
    s.connect();
  }
  return s;
}

export function disconnectSocket(): void {
  if (socket?.connected) {
    socket.disconnect();
  }
}

export function joinProjectRoom(projectId: string): void {
  const s = getSocket();
  if (s.connected) {
    s.emit("join", { project_id: projectId });
  }
}

export function leaveProjectRoom(projectId: string): void {
  const s = getSocket();
  if (s.connected) {
    s.emit("leave", { project_id: projectId });
  }
}

export function sendChatMessage(projectId: string, message: string): void {
  const s = getSocket();
  if (s.connected) {
    s.emit("chat:message", { project_id: projectId, message });
  }
}
