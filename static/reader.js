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

  // Auto-size the email iframe to its content so the page reads as one
  // continuous scroll (no nested scrollbar). Requires allow-same-origin in
  // the iframe sandbox — safe because the inner CSP still forbids scripts,
  // so the iframe can't run JS to exploit same-origin access.
  const iframe = document.getElementById('email-body');
  if (iframe) {
    const resize = () => {
      try {
        const doc = iframe.contentDocument;
        if (!doc) return;
        const h = Math.max(
          doc.documentElement.scrollHeight,
          doc.body ? doc.body.scrollHeight : 0
        );
        if (h > 0) iframe.style.height = h + 'px';
      } catch (_) { /* cross-origin or not loaded yet */ }
    };
    iframe.addEventListener('load', () => {
      resize();
      try {
        const ro = new ResizeObserver(resize);
        if (iframe.contentDocument?.body) ro.observe(iframe.contentDocument.body);
      } catch (_) { /* ResizeObserver unsupported — fall back to image-load hook */ }
      iframe.contentDocument?.querySelectorAll('img').forEach((img) => {
        if (!img.complete) img.addEventListener('load', resize);
      });
    });
  }
});

// --- Unread-dot optimistic update ---
// When the user clicks a card, note its href in sessionStorage. On pageshow
// (including bfcache restore, which hands back the stale landing DOM),
// strip the unread dot from any card whose href matches a visited entry.
// Dwell-timer on the article page still does the server-side mark_read; this
// is purely a UI sync for the "browser back" path where the stale page would
// otherwise still show the blue dot until a hard refresh.
const VISITED_KEY = 'email2rss.visitedArticles';
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.article-card[href]').forEach((card) => {
    card.addEventListener('click', () => {
      try {
        const visited = JSON.parse(sessionStorage.getItem(VISITED_KEY) || '[]');
        if (!visited.includes(card.getAttribute('href'))) {
          visited.push(card.getAttribute('href'));
          sessionStorage.setItem(VISITED_KEY, JSON.stringify(visited));
        }
      } catch (_) { /* sessionStorage unavailable — dwell timer + refresh still works */ }
    });
  });
});
window.addEventListener('pageshow', () => {
  try {
    const visited = JSON.parse(sessionStorage.getItem(VISITED_KEY) || '[]');
    if (!visited.length) return;
    const hrefs = new Set(visited);
    document.querySelectorAll('.article-card[href]').forEach((card) => {
      if (hrefs.has(card.getAttribute('href'))) {
        const dot = card.querySelector('.article-card__unread-dot');
        if (dot) dot.remove();
      }
    });
  } catch (_) { /* ignore — stale DOM still readable */ }
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
