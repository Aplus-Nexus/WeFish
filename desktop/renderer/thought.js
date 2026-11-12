const textarea = document.getElementById('thought');
const context = document.getElementById('context');

function send() {
  const value = textarea.value.trim();
  if (!value) return;
  window.wefish.submitThought(value);
  textarea.value = '';
}

document.getElementById('send').addEventListener('click', send);
document.getElementById('close').addEventListener('click', () => window.wefish.cancelThought());
textarea.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    send();
  }
});
window.wefish.onThoughtContext((value) => {
  const book = String(value?.bookTitle || '当前书籍');
  const chapter = String(value?.chapterName || '当前章节');
  const line = String(value?.line || '').replace(/\s+/g, ' ').trim();
  context.textContent = `${book} · ${chapter}${line ? ` · “${line.slice(0, 32)}${line.length > 32 ? '…' : ''}”` : ''}`;
  textarea.focus();
});
