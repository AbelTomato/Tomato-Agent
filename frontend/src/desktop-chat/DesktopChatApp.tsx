export function DesktopChatApp() {
  return (
    <main className="desktop-chat-shell" aria-label="普通聊天">
      <aside className="desktop-chat-sidebar">
        <h1>Tomato Agent</h1>
        <button type="button" disabled>新建对话</button>
        <p>尚无对话</p>
      </aside>
      <section className="desktop-chat-main" aria-label="聊天内容">
        <div>
          <h2>开始一段对话</h2>
          <p>模型尚未配置</p>
        </div>
        <form onSubmit={event => event.preventDefault()}>
          <textarea aria-label="消息" placeholder="输入消息" disabled />
          <button type="submit" disabled>发送</button>
        </form>
      </section>
    </main>
  );
}