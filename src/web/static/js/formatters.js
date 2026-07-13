/**
 * Formatters - Common formatting utilities
 */

const Formatters = {
    /**
     * Format number with commas
     */
    number(num) {
        if (num === null || num === undefined) return '0';
        return num.toLocaleString();
    },

    /**
     * Escape HTML to prevent XSS
     */
    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    /**
     * Format task name for display (convert snake_case to Title Case)
     */
    taskName(name) {
        if (!name) return '-';
        return name
            .replace(/_/g, ' ')
            .replace(/\b\w/g, c => c.toUpperCase());
    },

    /**
     * Format task value for display
     */
    taskValue(value, labels = null) {
        if (!value) return '-';
        
        // Use custom label if available
        if (labels && typeof labels === 'object' && labels[value]) {
            if (labels[value].label) {
                return labels[value].label;
            }
        }
        
        if (typeof value !== 'string') return String(value);
        // Truncate long values
        if (value.length > 25) {
            return value.substring(0, 22) + '...';
        }
        return value;
    },

    /**
     * Normalize a session title for display.
     */
    sessionTitle(value, fallback = '', maxLength = 120) {
        let text = String(value || fallback || '').trim();
        if (!text) return '';

        const firstNonEmptyLine = text.split(/\r?\n/).find(line => line.trim() !== '');
        if (firstNonEmptyLine) {
            text = firstNonEmptyLine.trim();
        }

        text = text.replace(/\s+/g, ' ').trim();
        if (text.length <= maxLength) return text;
        return text.slice(0, Math.max(1, maxLength - 1)).trimEnd() + '…';
    },

    /**
     * Get description for a task value from custom labels
     */
    getTaskValueDescription(value, labels = null) {
        if (labels && labels[value] && labels[value].description) {
            return labels[value].description;
        }
        return null;
    },

    /**
     * Highlight search term in text (case-insensitive)
     */
    highlightText(html, searchTerm) {
        if (!searchTerm || searchTerm.trim() === '') return html;
        
        const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const regex = new RegExp(`(?![^<]*>)(${escapedTerm})`, 'gi');
        return html.replace(regex, '<span class="search-highlight">$1</span>');
    },

    /**
     * Render markdown text to HTML (synchronous, no ML classification)
     */
    renderMarkdown(text, searchTerm = '') {
        if (!text) return '';
        try {
            marked.setOptions({
                breaks: true,
                gfm: true,
                headerIds: false,
                mangle: false
            });
            let html = marked.parse(text);
            
            if (searchTerm) {
                html = Formatters.highlightText(html, searchTerm);
            }
            
            // Normalize HTML to ensure all tags are properly closed
            // This prevents unclosed tags from swallowing subsequent content
            const div = document.createElement('div');
            div.innerHTML = html;
            return div.innerHTML;

        } catch (error) {
            console.error('Markdown rendering error:', error);
            return Formatters.escapeHtml(text);
        }
    }
};

// Export for use in other modules
window.Formatters = Formatters;
