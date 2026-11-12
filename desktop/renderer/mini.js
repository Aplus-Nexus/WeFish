const line = document.getElementById('mini-line');

window.wefish.onMiniUpdate((value) => {
  const text = String(value?.text || '').replace(/[\t\n\r ]+/g, ' ').trim();
  line.textContent = text;
  document.body.dataset.widthMode = String(value?.widthMode || 'dynamic');
});

window.wefish.miniReady();
