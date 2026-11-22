export function validDocumentSender(sender, binding) {
  if (!binding || !sender.tab || sender.tab.id !== binding.tabId || sender.frameId !== 0 || sender.documentId !== binding.documentId) return false;
  try { const u = new URL(sender.url); return u.origin === "https://web.whatsapp.com" && u.username === "" && u.password === ""; } catch { return false; }
}

export function validHello(result, session) {
  const validState = ["PAIRING_REQUIRED","READY_READ_ONLY","READY_SUPERVISED"].includes(result?.state);
  const sendOk = result?.state === "READY_SUPERVISED" ? typeof result.can_send === "boolean" : result?.can_send === false;
  return result?.protocol === "jarvis.bridge.v1" && result.operation === "hello_result" && result.session_id === session && validState && sendOk && typeof result.can_read_chats === "boolean";
}
