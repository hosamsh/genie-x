(function () {
    const STORAGE_KEY = 'genie-ui-theme';

    function readStoredTheme() {
        try {
            return window.localStorage.getItem(STORAGE_KEY) === 'light' ? 'light' : 'dark';
        } catch (_error) {
            return 'dark';
        }
    }

    function writeStoredTheme(theme) {
        try {
            window.localStorage.setItem(STORAGE_KEY, theme);
        } catch (_error) {
            return;
        }
    }

    function getTheme() {
        return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
    }

    function syncToggleState(theme) {
        document.querySelectorAll('[data-theme-option]').forEach((button) => {
            const isActive = button.dataset.themeOption === theme;
            button.classList.toggle('active', isActive);
            button.setAttribute('aria-pressed', String(isActive));
        });
    }

    function applyTheme(theme) {
        const normalizedTheme = theme === 'light' ? 'light' : 'dark';
        document.documentElement.dataset.theme = normalizedTheme;
        document.documentElement.classList.toggle('dark', normalizedTheme === 'dark');
        writeStoredTheme(normalizedTheme);
        syncToggleState(normalizedTheme);
    }

    function buildToggle() {
        const wrapper = document.createElement('div');
        wrapper.id = 'theme-toggle';
        wrapper.className = 'theme-toggle';
        wrapper.setAttribute('role', 'group');
        wrapper.setAttribute('aria-label', 'Color theme');
        wrapper.innerHTML = [
            '<button type="button" class="theme-toggle-option" data-theme-option="light" aria-pressed="false" title="Switch to day mode">',
            '<span class="material-symbols-outlined" aria-hidden="true">light_mode</span>',
            '</button>',
            '<button type="button" class="theme-toggle-option" data-theme-option="dark" aria-pressed="false" title="Switch to night mode">',
            '<span class="material-symbols-outlined" aria-hidden="true">dark_mode</span>',
            '</button>',
        ].join('');

        wrapper.addEventListener('click', (event) => {
            const button = event.target.closest('[data-theme-option]');
            if (!button) {
                return;
            }
            applyTheme(button.dataset.themeOption);
        });

        return wrapper;
    }

    function ensureThemeToggle() {
        const headerActions = document.querySelector('.header-actions');
        if (!headerActions || document.getElementById('theme-toggle')) {
            return;
        }

        headerActions.prepend(buildToggle());
        syncToggleState(getTheme());
    }

    document.addEventListener('DOMContentLoaded', () => {
        applyTheme(readStoredTheme());
        ensureThemeToggle();
    });

    window.addEventListener('storage', (event) => {
        if (event.key === STORAGE_KEY) {
            applyTheme(readStoredTheme());
        }
    });

    window.ThemeController = {
        ensureThemeToggle,
        getTheme,
        setTheme: applyTheme,
    };
})();