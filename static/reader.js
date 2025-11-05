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
        img.addEventListener('load', function() {
            img.style.opacity = '1';
        });
        img.style.opacity = '0';
        img.style.transition = 'opacity 0.3s ease-in';
    });

})();
