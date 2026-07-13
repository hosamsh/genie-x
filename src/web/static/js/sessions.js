/**
 * Sessions - Session listing and filtering
 */

const Sessions = {
    list: [],
    currentId: null,
    searchTerm: '',

    /**
     * Load sessions for a workspace
     */
    async load(workspaceId) {
        const response = await fetch(`/api/browse/workspace/${workspaceId}/sessions`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        this.list = data.sessions || [];
        return data.agents || [];
    },

    /**
     * Filter sessions by search term
     */
    filter(searchTerm) {
        this.searchTerm = (searchTerm || '').toLowerCase().trim();
        
        // Show/hide clear button
        const clearBtn = document.getElementById('session-search-clear');
        if (clearBtn) {
            clearBtn.classList.toggle('hidden', !this.searchTerm);
        }
        
        if (this.list.length === 0) return [];
        
        // Filter sessions
        const filtered = this.list.filter(session => {
            if (!this.searchTerm) return true;
            const sessionName = (session.session_name || session.session_id || '').toLowerCase();
            return sessionName.includes(this.searchTerm);
        });
        
        // Update count
        const countEl = document.getElementById('session-count');
        if (countEl) {
            countEl.textContent = this.searchTerm 
                ? `${filtered.length}/${this.list.length}` 
                : this.list.length;
        }
        
        return filtered;
    },

    /**
     * Clear search
     */
    clearSearch() {
        this.searchTerm = '';
        const searchInput = document.getElementById('session-search');
        if (searchInput) {
            searchInput.value = '';
            searchInput.blur();
        }
        return this.filter('');
    },

    /**
     * Render sessions list
     */
    render(container, sessions) {
        if (!container) return;
        
        // Hide search box if there are 5 or fewer total sessions
        const searchContainer = document.getElementById('session-search-container');
        if (searchContainer) {
            searchContainer.classList.toggle('hidden', this.list.length <= 5);
        }
        
        if (sessions.length === 0) {
            container.innerHTML = `
                <div class="px-3 py-4 text-center text-terminal-gray text-sm font-mono">
                    ${this.searchTerm ? 'No matching sessions' : 'No sessions'}
                </div>
            `;
            return;
        }
        
        container.innerHTML = sessions.map(session => {
            const date = session.first_timestamp ? new Date(session.first_timestamp) : null;
            const dateStr = date ? date.toLocaleDateString([], {month: 'short', day: 'numeric'}) : '';
            
            // Agent badge
            const info = AgentInfo.get(session.agent || 'default');
            
            // Highlight session name
            const sessionTitle = Formatters.sessionTitle(session.session_name, session.session_id.substring(0, 12));
            let sessionDisplayName = Formatters.escapeHtml(sessionTitle);
            if (this.searchTerm) {
                sessionDisplayName = Formatters.highlightText(sessionDisplayName, this.searchTerm);
            }
            
            return `
                <div class="session-item px-3 py-2 ${this.currentId === session.session_id ? 'active' : ''}" 
                     data-session-id="${session.session_id}"
                     onclick="WorkspacePage.selectSession('${session.session_id}')">
                    <div class="flex items-center gap-1.5 mb-1">
                        <img src="${info.logo}" alt="${info.name}" class="w-3 h-3 object-contain flex-shrink-0" onerror="this.src='/static/img/agent-logo.svg'">
                        <div class="font-medium text-sm text-white truncate font-mono" title="${sessionTitle}">
                            ${sessionDisplayName}
                        </div>
                        ${session.session_origin_label ? `<span class="px-1.5 py-0.5 rounded border border-border-dark text-[10px] uppercase tracking-wide text-terminal-cyan flex-shrink-0">${Formatters.escapeHtml(session.session_origin_label)}</span>` : ''}
                    </div>
                    <div class="flex items-center justify-between text-xs text-terminal-gray font-mono">
                        <span>${dateStr}</span>
                        <span>${session.turn_count} turns${session.subagent_count ? ` • ${session.subagent_count} subagents` : ''}</span>
                    </div>
                </div>
            `;
        }).join('');
    },

    /**
     * Render session stats bar
     */
    renderStats(session) {
        const container = document.getElementById('session-stats');
        if (!session || !container) return;
        
        const stats = [];
        
        // Turn count
        stats.push(`<span><strong>${session.turn_count}</strong> turns</span>`);
        
        // Code metrics
        if (session.total_lines_added > 0 || session.total_lines_removed > 0) {
            const parts = [];
            if (session.total_lines_added > 0) {
                parts.push(`<span class="text-terminal-green">+${Formatters.number(session.total_lines_added)}</span>`);
            }
            if (session.total_lines_removed > 0) {
                parts.push(`<span class="text-error">-${Formatters.number(session.total_lines_removed)}</span>`);
            }
            stats.push(`<span>LOC: ${parts.join(' ')}</span>`);
        }
        
        // Files edited
        if (session.total_files_edited > 0) {
            stats.push(`<span><strong>${session.total_files_edited}</strong> files edited</span>`);
        }
        
        // Languages
        if (session.languages && session.languages.length > 0) {
            const langList = session.languages.slice(0, 3).join(', ');
            const more = session.languages.length > 3 ? ` +${session.languages.length - 3}` : '';
            stats.push(`<span class="text-purple-400">${langList}${more}</span>`);
        }
        
        // Agent
        const info = AgentInfo.get(session.agent || 'unknown');
        stats.push(`<span class="flex items-center gap-1">${AgentInfo.renderLogo(session.agent)} ${info.name}</span>`);
        
        container.innerHTML = stats.join('<span class="text-border-dark">•</span>');
    },

    /**
     * Get session by ID
     */
    getById(sessionId) {
        return this.list.find(s => s.session_id === sessionId);
    }
};

// Export for use in other modules
window.Sessions = Sessions;
