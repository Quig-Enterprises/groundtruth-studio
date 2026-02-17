/**
 * Groundtruth Studio - Responsive Utilities
 * Shared mobile-first helpers: hamburger nav, active link detection, resize handling
 */
(function() {
    'use strict';

    // ===== Hamburger Navigation =====

    function initHamburgerNav() {
        const nav = document.getElementById('site-nav');
        const btn = document.getElementById('hamburger-btn');
        const links = document.getElementById('nav-links');

        if (!nav || !btn || !links) return;

        // Toggle menu
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            const isOpen = nav.classList.toggle('nav-open');
            btn.setAttribute('aria-expanded', isOpen);
        });

        // Close menu when clicking a nav link
        links.addEventListener('click', function(e) {
            if (e.target.classList.contains('nav-link')) {
                nav.classList.remove('nav-open');
                btn.setAttribute('aria-expanded', 'false');
            }
        });

        // Close menu when clicking outside
        document.addEventListener('click', function(e) {
            if (!nav.contains(e.target) && nav.classList.contains('nav-open')) {
                nav.classList.remove('nav-open');
                btn.setAttribute('aria-expanded', 'false');
            }
        });

        // Close menu on Escape key
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && nav.classList.contains('nav-open')) {
                nav.classList.remove('nav-open');
                btn.setAttribute('aria-expanded', 'false');
                btn.focus();
            }
        });

        // Close menu when viewport crosses desktop breakpoint
        const desktopQuery = window.matchMedia('(min-width: 768px)');
        desktopQuery.addEventListener('change', function(e) {
            if (e.matches) {
                nav.classList.remove('nav-open');
                btn.setAttribute('aria-expanded', 'false');
            }
        });
    }

    // ===== Dropdown Toggles =====

    function initDropdownToggles() {
        var toggles = document.querySelectorAll('.nav-dropdown-toggle');
        toggles.forEach(function(toggle) {
            toggle.addEventListener('click', function(e) {
                e.preventDefault();
                e.stopPropagation();
                var dropdown = toggle.closest('.nav-dropdown');
                var wasOpen = dropdown.classList.contains('open');
                // Close all other dropdowns
                document.querySelectorAll('.nav-dropdown.open').forEach(function(d) {
                    d.classList.remove('open');
                });
                // Toggle this one
                if (!wasOpen) {
                    dropdown.classList.add('open');
                }
            });
        });

        // Close dropdowns when clicking outside
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.nav-dropdown')) {
                document.querySelectorAll('.nav-dropdown.open').forEach(function(d) {
                    d.classList.remove('open');
                });
            }
        });
    }

    // ===== Active Link Detection =====

    function setActiveNavLink() {
        const currentPath = window.location.pathname;
        const navLinks = document.querySelectorAll('.nav-link');

        navLinks.forEach(function(link) {
            const href = link.getAttribute('href');
            if (href === currentPath || (href === '/' && currentPath === '/')) {
                link.classList.add('active');
            } else if (href !== '/' && currentPath.startsWith(href)) {
                link.classList.add('active');
            }
        });
    }

    // ===== Initialize =====

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            initHamburgerNav();
            initDropdownToggles();
            setActiveNavLink();
        });
    } else {
        initHamburgerNav();
        initDropdownToggles();
        setActiveNavLink();
    }
})();
