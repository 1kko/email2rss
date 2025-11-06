// Email2RSS Internal Reader - Minimal JavaScript Enhancements

(function() {
    'use strict';

    // Add print functionality
    document.addEventListener('keydown', function(e) {
        if ((e.ctrlKey || e.metaKey) && e.key === 'p') {
            e.preventDefault();
            window.print();
        }
    });

    // Progressive enhancement: Add loading indicator for images
    document.querySelectorAll('.content img').forEach(function(img) {
        // Only apply fade-in effect if image is not already loaded
        if (img.complete) {
            // Image already loaded (cached), show it immediately
            img.style.opacity = '1';
        } else {
            // Image still loading, fade it in when ready
            img.style.opacity = '0';
            img.style.transition = 'opacity 0.3s ease-in';
            img.addEventListener('load', function() {
                img.style.opacity = '1';
            });
        }
    });

})();
