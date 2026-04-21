// Reader UI: dwell-timer auto-read, star toggle, mark-unread button.
// Runs on the outer article page (not inside the sandboxed iframe, where scripts are blocked).

document.addEventListener('DOMContentLoaded', () => {
  const article = document.querySelector('.article[data-feed]');
  if (!article) return;

  const feed = article.dataset.feed;
  const guid = article.dataset.guid;
  const readAfter = Number(article.dataset.readAfterSeconds) || 5;
  const isReadInitially = article.dataset.isRead === 'true';

  // Dwell timer — fire once if not already read
  if (!isReadInitially) {
    setTimeout(() => {
      fetch(`/article/${feed}/${guid}/read`, {
        method: 'POST',
        credentials: 'same-origin',
      }).catch((err) => console.warn('mark-read failed', err));
    }, readAfter * 1000);
  }

  // Star toggle
  const starBtn = document.getElementById('star-btn');
  if (starBtn) {
    starBtn.addEventListener('click', async () => {
      const currentlyStarred = article.dataset.isStarred === 'true';
      const method = currentlyStarred ? 'DELETE' : 'POST';
      try {
        const resp = await fetch(`/article/${feed}/${guid}/star`, {
          method,
          credentials: 'same-origin',
        });
        if (resp.ok) {
          const data = await resp.json();
          article.dataset.isStarred = data.is_starred ? 'true' : 'false';
          const icon = starBtn.querySelector('.star-icon');
          if (icon) icon.textContent = data.is_starred ? '★' : '☆';
        }
      } catch (err) {
        console.warn('star toggle failed', err);
      }
    });
  }

  // Mark-unread — fires DELETE then redirects back to the article list
  const unreadBtn = document.getElementById('unread-btn');
  if (unreadBtn) {
    unreadBtn.addEventListener('click', async () => {
      try {
        await fetch(`/article/${feed}/${guid}/read`, {
          method: 'DELETE',
          credentials: 'same-origin',
        });
      } catch (err) {
        console.warn('mark-unread failed', err);
      }
      window.location.href = '/article';
    });
  }
});

// --- Landing page scroller wiring (Netflix-style horizontal rows) ---
// For each .feed-row__scroller: drive arrows, edge-fade opacity via data attrs.
// Runs on / (landing) only; has no effect on other pages.
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.feed-row__scroller').forEach((scroller) => {
    const track = scroller.querySelector('.feed-row__track');
    const prev = scroller.querySelector('.feed-row__arrow--prev');
    const next = scroller.querySelector('.feed-row__arrow--next');
    if (!track || !prev || !next) return;

    function cardStep() {
      const firstCard = track.querySelector('.article-card');
      if (!firstCard) return 260;
      return firstCard.offsetWidth + 18; // gap 1.1rem ≈ 18px
    }

    function updateState() {
      const atStart = track.scrollLeft <= 1;
      const atEnd = track.scrollLeft + track.clientWidth >= track.scrollWidth - 1;
      scroller.dataset.atStart = atStart;
      scroller.dataset.atEnd = atEnd;
      prev.disabled = atStart;
      next.disabled = atEnd;
    }

    prev.addEventListener('click', () => {
      track.scrollBy({ left: -cardStep() * 2, behavior: 'smooth' });
    });
    next.addEventListener('click', () => {
      track.scrollBy({ left: cardStep() * 2, behavior: 'smooth' });
    });
    track.addEventListener('scroll', updateState, { passive: true });
    window.addEventListener('resize', updateState);
    requestAnimationFrame(updateState);
  });
});
