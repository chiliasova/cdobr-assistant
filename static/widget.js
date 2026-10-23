// Поп-ап виджет для корпоративного портала.
// Подключение: <script src="http://<сервер>:8000/static/widget.js"></script>
(function () {
  const base = new URL(document.currentScript.src).origin;
  const btn = document.createElement("button");
  btn.textContent = "?";
  btn.title = "Помощник по документообороту";
  btn.style.cssText = "position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;" +
    "background:#1f5fbf;color:#fff;font-size:24px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.2);z-index:99999";
  const frame = document.createElement("iframe");
  frame.src = base + "/?widget=1";
  frame.style.cssText = "position:fixed;right:20px;bottom:88px;width:400px;max-width:calc(100vw - 32px);height:600px;" +
    "max-height:calc(100vh - 120px);border:1px solid #e3e6eb;border-radius:12px;background:#fff;" +
    "box-shadow:0 8px 32px rgba(0,0,0,.18);z-index:99999;display:none";
  btn.onclick = () => (frame.style.display = frame.style.display === "none" ? "block" : "none");
  document.body.append(frame, btn);
})();
