/**
 * Workspace Page Controller - Main orchestrator for the workspace detail page
 * Uses modular components: Sessions, Turns, Modals, etc.
 */

const WorkspacePage = {
    // State
    workspaceId: '',
    workspaceAgents: [],
    workspaceStatus: null,
    dashboardStats: null,
    
    // Current view
    currentView: 'dashboard',  // 'dashboard' or 'session'

    /**
     * Return the deep-linked session id from the current URL, if any.
     */
    getDeepLinkedSessionId() {
        const pathParts = window.location.pathname.split('/');
        const view = pathParts[3];
        const sessionId = pathParts[4];
        if (view === 'session' && sessionId) {
            return sessionId;
        }
        return null;
    },

    /**
     * Initialize the workspace page
     */
    init() {
        // Parse URL: /workspace/{workspace_id}
        const pathParts = window.location.pathname.split('/');
        this.workspaceId = pathParts[2];
        
        // Hide the single agent badge
        const agentBadge = document.getElementById('agent-badge');
        if (agentBadge) agentBadge.classList.add('hidden');
        
        // Update content height on load and resize
        this.updateContentHeight();
        window.addEventListener('resize', () => this.updateContentHeight());
        
        // Initialize workspace selector
        this.initWorkspaceSelector();
        
        // Initialize history state management
        this.initHistoryHandling();
        
        // NOTE: Do NOT call restoreViewFromUrl() here!
        // The deep-link session view is restored in loadWorkspaceStatus() after
        // sessions are loaded and main content is visible. Otherwise, turn text
        // clamps won't work because scrollHeight=0 when content is hidden.
        
        this.loadWorkspaceStatus();
    },

    /**
     * Initialize history handling for back/forward buttons
     */
    initHistoryHandling() {
        window.addEventListener('popstate', (e) => {
            this.restoreViewFromUrl();
        });
    },

    /**
     * Build URL for current state
     */
    buildCurrentUrl() {
        let url = `/workspace/${this.workspaceId}`;
        if (this.currentView === 'session' && Sessions.currentId) {
            url += `/session/${Sessions.currentId}`;
        }
        return url;
    },

    /**
     * Update browser history when view changes
     */
    updateHistory() {
        const url = this.buildCurrentUrl();
        window.history.pushState(
            { 
                view: this.currentView, 
                sessionId: Sessions.currentId,
                workspaceId: this.workspaceId
            },
            '',
            url
        );
    },

    /**
     * Restore view state from URL
     */
    restoreViewFromUrl() {
        const sessionId = this.getDeepLinkedSessionId();
        
        if (sessionId) {
            // Restore session view
            Sessions.currentId = sessionId;
            this.selectSession(sessionId, true); // true = skipHistory (we're already in history)
        } else {
            // Restore dashboard view
            this.showDashboard(true); // true = skipHistory
        }
    },

    /**
     * Initialize workspace selector dropdown
     */
    initWorkspaceSelector() {
        const trigger = document.getElementById('workspace-selector-trigger');
        const dropdown = document.getElementById('workspace-dropdown');
        
        if (!trigger || !dropdown) return;
        
        // Track highlighted index for keyboard navigation
        this.highlightedWorkspaceIndex = -1;
        
        // Toggle dropdown on click
        trigger.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = dropdown.classList.contains('open');
            
            if (isOpen) {
                this.closeWorkspaceSelector();
            } else {
                this.openWorkspaceSelector();
            }
        });
        
        // Close dropdown when clicking outside
        document.addEventListener('click', (e) => {
            const isOpen = dropdown.classList.contains('open');
            if (isOpen && !trigger.contains(e.target) && !dropdown.contains(e.target)) {
                this.closeWorkspaceSelector();
            }
        });
        
        // Close dropdown on Escape key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && dropdown.classList.contains('open')) {
                this.closeWorkspaceSelector();
            }
        });
        
        // Handle search input
        dropdown.addEventListener('input', (e) => {
            if (e.target.id === 'workspace-search-input') {
                this.filterWorkspaceSelector(e.target.value);
            }
        });
        
        // Keyboard navigation in search box
        dropdown.addEventListener('keydown', (e) => {
            if (e.target.id === 'workspace-search-input') {
                this.handleWorkspaceSelectorKeyboard(e);
            }
        });
        
        // Prevent dropdown clicks from closing it
        dropdown.addEventListener('click', (e) => {
            e.stopPropagation();
        });
    },

    /**
     * Handle keyboard navigation in workspace selector
     */
    handleWorkspaceSelectorKeyboard(e) {
        const items = document.querySelectorAll('.workspace-dropdown-item');
        if (items.length === 0) return;
        
        const maxIndex = items.length - 1;
        
        switch(e.key) {
            case 'ArrowDown':
                e.preventDefault();
                this.highlightedWorkspaceIndex = Math.min(this.highlightedWorkspaceIndex + 1, maxIndex);
                this.updateWorkspaceHighlight(items);
                break;
                
            case 'ArrowUp':
                e.preventDefault();
                this.highlightedWorkspaceIndex = Math.max(this.highlightedWorkspaceIndex - 1, 0);
                this.updateWorkspaceHighlight(items);
                break;
                
            case 'Tab':
                e.preventDefault();
                if (e.shiftKey) {
                    // Shift+Tab goes up
                    this.highlightedWorkspaceIndex = Math.max(this.highlightedWorkspaceIndex - 1, 0);
                } else {
                    // Tab goes down
                    this.highlightedWorkspaceIndex = Math.min(this.highlightedWorkspaceIndex + 1, maxIndex);
                }
                this.updateWorkspaceHighlight(items);
                break;
                
            case 'Enter':
                e.preventDefault();
                if (this.highlightedWorkspaceIndex >= 0 && this.highlightedWorkspaceIndex < items.length) {
                    const item = items[this.highlightedWorkspaceIndex];
                    const workspaceId = item.dataset.workspaceId;
                    if (workspaceId && workspaceId !== this.workspaceId) {
                        window.location.href = `/workspace/${workspaceId}`;
                    }
                }
                break;
        }
    },

    /**
     * Update visual highlight for keyboard navigation
     */
    updateWorkspaceHighlight(items) {
        // Remove all highlights
        items.forEach(item => item.classList.remove('highlighted'));
        
        // Add highlight to current item
        if (this.highlightedWorkspaceIndex >= 0 && this.highlightedWorkspaceIndex < items.length) {
            const item = items[this.highlightedWorkspaceIndex];
            item.classList.add('highlighted');
            
            // Scroll into view if needed
            item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        }
    },

    /**
     * Open workspace selector and load workspaces
     */
    async openWorkspaceSelector() {
        const trigger = document.getElementById('workspace-selector-trigger');
        const dropdown = document.getElementById('workspace-dropdown');
        
        if (!trigger || !dropdown) return;
        
        trigger.classList.add('open');
        dropdown.classList.add('open');
        
        // Load workspaces if not already loaded
        await this.loadWorkspacesForSelector();
    },

    /**
     * Close workspace selector
     */
    closeWorkspaceSelector() {
        const trigger = document.getElementById('workspace-selector-trigger');
        const dropdown = document.getElementById('workspace-dropdown');
        
        if (trigger) trigger.classList.remove('open');
        if (dropdown) dropdown.classList.remove('open');
    },

    /**
     * Load all workspaces for the selector dropdown
     */
    async loadWorkspacesForSelector() {
        const dropdown = document.getElementById('workspace-dropdown');
        if (!dropdown) return;
        
        try {
            dropdown.innerHTML = '<div class="workspace-dropdown-loading">Loading workspaces...</div>';
            
            const response = await fetch('/api/browse/workspaces?page=1&page_size=500&include_live=true');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            
            const data = await response.json();
            const workspaces = data.workspaces || [];
            
            if (workspaces.length === 0) {
                dropdown.innerHTML = '<div class="workspace-dropdown-empty">No workspaces found</div>';
                return;
            }
            
            // Store workspaces for filtering
            this.allWorkspaces = workspaces;
            
            // Render with search box
            this.renderWorkspaceDropdown(workspaces);
            
            // Focus search input
            const searchInput = document.getElementById('workspace-search-input');
            if (searchInput) {
                setTimeout(() => searchInput.focus(), 100);
            }
        } catch (error) {
            console.error('Failed to load workspaces:', error);
            dropdown.innerHTML = '<div class="workspace-dropdown-empty">Failed to load workspaces</div>';
        }
    },

    /**
     * Render workspace dropdown with search and items
     */
    renderWorkspaceDropdown(workspaces) {
        const dropdown = document.getElementById('workspace-dropdown');
        if (!dropdown) return;
        
        const searchBox = `
            <div class="workspace-dropdown-search">
                <input type="text" 
                       id="workspace-search-input" 
                       placeholder="Search workspaces..." 
                       autocomplete="off">
            </div>
        `;
        
        const itemsHtml = workspaces.map(ws => this.renderWorkspaceItem(ws)).join('');
        const itemsContainer = `<div class="workspace-dropdown-items">${itemsHtml}</div>`;
        
        dropdown.innerHTML = searchBox + itemsContainer;
        
        // Attach click handlers
        dropdown.querySelectorAll('.workspace-dropdown-item').forEach(item => {
            item.addEventListener('click', () => {
                const workspaceId = item.dataset.workspaceId;
                if (workspaceId && workspaceId !== this.workspaceId) {
                    window.location.href = `/workspace/${workspaceId}`;
                }
            });
        });
    },

    /**
     * Filter workspaces in the dropdown
     */
    filterWorkspaceSelector(searchTerm) {
        if (!this.allWorkspaces) return;
        
        const term = searchTerm.toLowerCase().trim();
        
        // Reset highlighted index when filtering
        this.highlightedWorkspaceIndex = -1;
        
        if (!term) {
            // Show all workspaces
            this.renderWorkspaceDropdown(this.allWorkspaces);
            return;
        }
        
        // Filter workspaces by name or agent
        const filtered = this.allWorkspaces.filter(ws => {
            const workspaceId = ws.workspace_id || ws.id;
            const workspaceName = (ws.workspace_name || workspaceId).toLowerCase();
            const agents = (ws.agents || []).map(a => a.toLowerCase());
            
            return workspaceName.includes(term) || agents.some(agent => agent.includes(term));
        });
        
        if (filtered.length === 0) {
            const dropdown = document.getElementById('workspace-dropdown');
            const searchBox = dropdown.querySelector('.workspace-dropdown-search');
            const emptyMessage = '<div class="workspace-dropdown-empty">No matching workspaces</div>';
            dropdown.innerHTML = searchBox.outerHTML + emptyMessage;
        } else {
            this.renderWorkspaceDropdown(filtered);
        }
        
        // Restore search value
        const searchInput = document.getElementById('workspace-search-input');
        if (searchInput) {
            searchInput.value = searchTerm;
            searchInput.focus();
        }
    },

    /**
     * Render a single workspace item in the dropdown
     */
    renderWorkspaceItem(workspace) {
        const workspaceId = workspace.workspace_id || workspace.id;
        const workspaceName = workspace.workspace_name || workspaceId;
        const agents = workspace.agents || [];
        const isActive = workspaceId === this.workspaceId;
        
        // Render agent icons
        const agentIcons = agents.map(agent => {
            const info = AgentInfo.get(agent);
            return `<img src="${info.logo}" 
                         alt="${info.name}" 
                         title="${info.name}"
                         class="w-4 h-4 object-contain" 
                         onerror="this.src='/static/img/agent-logo.svg'">`;
        }).join('');
        
        return `
            <div class="workspace-dropdown-item ${isActive ? 'active' : ''}" 
                 data-workspace-id="${workspaceId}"
                 title="${workspaceName}">
                <span class="workspace-dropdown-name font-mono">${workspaceName}</span>
                <div class="workspace-dropdown-agents">
                    ${agentIcons}
                </div>
            </div>
        `;
    },

    /**
     * Update the main content height based on visible headers
     */
    updateContentHeight() {
        const header = document.querySelector('header');
        const breadcrumb = document.querySelector('header')?.nextElementSibling;
        const extractionStatus = document.getElementById('extraction-status');
        const syncWarningBar = document.getElementById('sync-warning-bar');
        
        let headerHeight = 0;
        if (header) headerHeight += header.offsetHeight;
        if (breadcrumb) headerHeight += breadcrumb.offsetHeight;
        if (extractionStatus && !extractionStatus.classList.contains('hidden')) {
            headerHeight += extractionStatus.offsetHeight;
        }
        if (syncWarningBar && !syncWarningBar.classList.contains('hidden')) {
            headerHeight += syncWarningBar.offsetHeight;
        }
        
        document.documentElement.style.setProperty('--header-height', `${headerHeight}px`);
    },

    /**
     * Load workspace status and trigger extraction if needed
     */
    async loadWorkspaceStatus() {
        const deepLinkedSessionId = this.getDeepLinkedSessionId();
        if (deepLinkedSessionId) {
            this.preloadDeepLinkedSession(deepLinkedSessionId);
        } else {
            this.showLoading('Checking workspace status...');
        }
        
        try {
            const response = await fetch(`/api/browse/workspace/${this.workspaceId}/status`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            
            this.workspaceStatus = await response.json();
            this.workspaceAgents = this.workspaceStatus.agents || [];
            
            // Update workspace name
            const workspaceDisplayName = this.workspaceStatus.workspace_name || this.workspaceId;
            document.getElementById('workspace-name').textContent = workspaceDisplayName;

            // Also update the workspace name shown in the left nav (workspace-scoped section)
            const navWorkspaceName = document.getElementById('nav-workspace-name');
            if (navWorkspaceName) navWorkspaceName.textContent = workspaceDisplayName;
            
            this.updateAgentsBadges();
            
            if (this.workspaceStatus.is_extracted) {
                this.updateStatusBadge();
                if (deepLinkedSessionId) {
                    this.showMainContent();
                    void this.loadSessions();
                    void this.loadDashboard();
                } else {
                    this.showMainContent();
                    void this.loadSessions();
                    void this.loadDashboard();

                    // Keep URL state in sync after the main content is visible.
                    this.restoreViewFromUrl();
                }
                
                // Check if source has newer data
                if (this.workspaceStatus.source_available) {
                    setTimeout(() => void this.checkSyncStatus(), 2000);
                }
            } else {
                await this.extractWorkspace();
            }
            
        } catch (error) {
            console.error('Failed to load workspace status:', error);
            this.showExtractionStatus('error', 'Failed to Load', error.message, [
                { label: 'Retry', onclick: 'WorkspacePage.loadWorkspaceStatus()' },
                { label: 'Back to Browse', onclick: 'window.location.href="/browse"', secondary: true }
            ]);
        }
    },

    /**
     * Check if workspace source has new data since last extraction
     */
    async checkSyncStatus() {
        try {
            const response = await fetch(`/api/browse/workspace/${this.workspaceId}/sync-status`);
            if (!response.ok) return;
            
            const syncStatus = await response.json();
            
            if (syncStatus.needs_sync) {
                this.showSyncWarning(syncStatus);
            } else {
                this.hideSyncWarning();
            }
        } catch (error) {
            console.error('Failed to check sync status:', error);
        }
    },

    /**
     * Show the sync warning bar
     */
    showSyncWarning(syncStatus) {
        const bar = document.getElementById('sync-warning-bar');
        const message = document.getElementById('sync-warning-message');
        
        let parts = [];
        if (syncStatus.new_sessions > 0) {
            parts.push(`${syncStatus.new_sessions} new session${syncStatus.new_sessions > 1 ? 's' : ''}`);
        }
        if (syncStatus.new_turns > 0) {
            parts.push(`~${syncStatus.new_turns} new turn${syncStatus.new_turns > 1 ? 's' : ''}`);
        }
        
        message.textContent = parts.length > 0 
            ? `Found ${parts.join(' and ')} in your workspace`
            : syncStatus.message;
        
        bar.classList.remove('hidden');
        this.updateContentHeight();
    },

    /**
     * Hide the sync warning bar
     */
    hideSyncWarning() {
        document.getElementById('sync-warning-bar').classList.add('hidden');
        this.updateContentHeight();
    },

    /**
     * Sync workspace - extract new data
     */
    async syncWorkspace() {
        this.hideSyncWarning();
        await this.startStreamingExtraction(false, true);
    },

    /**
     * Update the agents badges in the header
     */
    updateAgentsBadges() {
        const container = document.getElementById('agents-container');
        if (!container) return;
        
        container.innerHTML = this.workspaceAgents.map(agent => AgentInfo.renderBadge(agent)).join(' ');
    },

    /**
     * Extract workspace
     */
    async extractWorkspace(deleteExisting = false, syncOnly = false) {
        await this.startStreamingExtraction(deleteExisting, syncOnly);
    },

    /**
     * Start streaming extraction with modal
     */
    async startStreamingExtraction(deleteExisting = false, sync = false) {
        Modals.showExtraction();
        
        try {
            const params = new URLSearchParams();
            if (deleteExisting) params.append('deleteExisting', 'true');
            if (sync) params.append('sync', 'true');
            const queryString = params.toString();
            const url = `/api/browse/workspace/${this.workspaceId}/extract-stream${queryString ? '?' + queryString : ''}`;
            
            const response = await fetch(url, { method: 'POST' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            
            const result = await response.json();
            
            if (!result.streaming) {
                Modals.appendExtractionLog(`✓ ${result.message || 'Workspace already extracted'}`, 'success');
                Modals.setExtractionState('completed');
                return;
            }
            
            SSEHandler.streamExtraction(result.run_id, {
                onComplete: () => this.closeExtractionModal(),
                onFailed: () => {},
                onError: () => {},
                onConnectionError: () => {}
            });
            
        } catch (error) {
            console.error('Failed to start extraction:', error);
            Modals.appendExtractionLog('Error: Failed to start extraction - ' + error.message, 'error');
            Modals.setExtractionState('failed');
        }
    },

    /**
     * Close extraction modal and load workspace data
     */
    async closeExtractionModal() {
        Modals.closeExtraction();
        await this.loadWorkspaceStatus();
    },

    /**
     * Refresh extraction
     */
    async refreshExtraction() {
        await this.extractWorkspace(false, true);
    },

    /**
     * Reextract workspace from source, overwriting the current extraction.
     */
    async reextractWorkspace() {
        const confirmed = window.confirm(
            'Reextract this workspace from source and overwrite the current extracted data?'
        );
        if (!confirmed) {
            return;
        }

        this.hideSyncWarning();
        await this.extractWorkspace(true, false);
    },

    /**
     * Load sessions
     */
    async loadSessions() {
        try {
            const container = document.getElementById('sessions-list');
            if (container) {
                container.innerHTML = `
                    <div class="px-3 py-4 text-center text-terminal-gray text-sm font-mono">
                        Loading sessions...
                    </div>
                `;
            }
            const agents = await Sessions.load(this.workspaceId);
            this.workspaceAgents = agents.length > 0 ? agents : this.workspaceAgents;
            this.renderSessions();
            if (Sessions.currentId) {
                this.updateSelectedSessionHeader(Sessions.currentId);
                document.querySelectorAll('.session-item').forEach(el => {
                    el.classList.toggle('active', el.dataset.sessionId === Sessions.currentId);
                });
            }
        } catch (error) {
            console.error('Failed to load sessions:', error);
        }
    },

    /**
     * Preload a deep-linked session immediately, without waiting for the slow
     * workspace sessions bootstrap.
     */
    preloadDeepLinkedSession(sessionId) {
        Sessions.currentId = sessionId;
        Turns.clearSearch();
        this.showMainContent();
        this.showSessionView(true);
        this.updateSelectedSessionHeader(sessionId);

        const mainContent = document.querySelector('.flex-1.overflow-y-auto');
        if (mainContent) mainContent.scrollTop = 0;

        void this.loadTurnsForSession(sessionId);
    },

    /**
     * Update the session title/subtitle/stats using whatever metadata is
     * currently available.
     */
    updateSelectedSessionHeader(sessionId) {
        const session = Sessions.getById(sessionId);
        document.getElementById('current-session-title').textContent =
            Formatters.sessionTitle(session?.session_name, sessionId.substring(0, 12));
        document.getElementById('current-session-subtitle').textContent =
            session?.first_timestamp ? new Date(session.first_timestamp).toLocaleString() : 'Loading session metadata...';

        const agentIconEl = document.getElementById('session-agent-icon');
        if (agentIconEl && session?.agent) {
            agentIconEl.innerHTML = AgentInfo.renderLogo(session.agent, 5);
        } else if (agentIconEl) {
            agentIconEl.innerHTML = '';
        }

        Sessions.renderStats(session);
    },

    /**
     * Load turns for a session and render them.
     */
    async loadTurnsForSession(sessionId) {
        try {
            await Turns.load(sessionId);
            if (!Sessions.getById(sessionId) && Turns.list.length > 0) {
                const firstTurn = Turns.list[0];
                if (firstTurn.session_name) {
                    document.getElementById('current-session-title').textContent = Formatters.sessionTitle(firstTurn.session_name, sessionId.substring(0, 12));
                }
                if (firstTurn.timestamp_iso) {
                    document.getElementById('current-session-subtitle').textContent = new Date(firstTurn.timestamp_iso).toLocaleString();
                }
                const agentIconEl = document.getElementById('session-agent-icon');
                if (agentIconEl && firstTurn.agent_used) {
                    agentIconEl.innerHTML = AgentInfo.renderLogo(firstTurn.agent_used, 5);
                }
            }
            this.renderTurns();
        } catch (error) {
            console.error('Failed to load turns:', error);
            document.getElementById('turns-container').innerHTML = `
                <div class="text-center text-red-500">Failed to load turns: ${error.message}</div>
            `;
        }
    },

    /**
     * Render sessions list
     */
    renderSessions() {
        const filtered = Sessions.filter(Sessions.searchTerm);
        const container = document.getElementById('sessions-list');
        Sessions.render(container, filtered);
    },

    /**
     * Filter sessions by search term
     */
    filterSessions(searchTerm) {
        const filtered = Sessions.filter(searchTerm);
        const container = document.getElementById('sessions-list');
        Sessions.render(container, filtered);
    },

    /**
     * Clear session search
     */
    clearSessionSearch() {
        const filtered = Sessions.clearSearch();
        const container = document.getElementById('sessions-list');
        Sessions.render(container, filtered);
    },

    /**
     * Select a session
     */
    async selectSession(sessionId, skipHistory = false) {
        // Update active state
        document.querySelectorAll('.session-item').forEach(el => {
            el.classList.toggle('active', el.dataset.sessionId === sessionId);
        });
        
        Sessions.currentId = sessionId;
        Turns.clearSearch();
        
        this.showSessionView(skipHistory);
        
        // Reset scroll
        const mainContent = document.querySelector('.flex-1.overflow-y-auto');
        if (mainContent) mainContent.scrollTop = 0;
        
        this.updateSelectedSessionHeader(sessionId);
        await this.loadTurnsForSession(sessionId);
    },

    /**
     * Render turns
     */
    renderTurns() {
        const filtered = Turns.filter(Turns.searchTerm);
        const container = document.getElementById('turns-container');
        Turns.render(container, filtered);
    },

    /**
     * Filter turns
     */
    filterTurns(searchTerm) {
        const filtered = Turns.filter(searchTerm);
        const container = document.getElementById('turns-container');
        Turns.render(container, filtered);
    },

    /**
     * Clear turn search
     */
    clearTurnSearch() {
        const filtered = Turns.clearSearch();
        const container = document.getElementById('turns-container');
        Turns.render(container, filtered);
    },

    /**
     * Export current session as Markdown
     */
    async exportSessionMarkdown() {
        const sessionId = Sessions.currentId;
        if (!sessionId) {
            console.warn('No session selected for export');
            return;
        }

        try {
            await Turns.load(sessionId);
        } catch (error) {
            console.error('Failed to load turns for export:', error);
            return;
        }

        const session = Sessions.getById(sessionId);
        const markdown = this.buildSessionMarkdown(sessionId, session, Turns.list || []);
        const filename = this.buildSessionMarkdownFilename(sessionId, session);
        this.downloadTextFile(markdown, filename, 'text/markdown');
    },

    /**
     * Build Markdown for a session
     */
    buildSessionMarkdown(sessionId, session, turns) {
        const title = session?.session_name || sessionId.substring(0, 12);
        const exportedAt = new Date().toISOString();
        const firstTimestamp = session?.first_timestamp
            ? new Date(session.first_timestamp).toLocaleString()
            : '';

        const lines = [];
        lines.push(`# ${title}`);
        lines.push('');
        lines.push(`- Session ID: ${sessionId}`);
        lines.push(`- Workspace ID: ${this.workspaceId}`);
        if (firstTimestamp) {
            lines.push(`- Session Start: ${firstTimestamp}`);
        }
        lines.push(`- Exported At: ${exportedAt}`);
        lines.push(`- Turn Count: ${turns.length}`);
        lines.push('');

        turns.forEach(turn => {
            const role = turn.role ? turn.role.charAt(0).toUpperCase() + turn.role.slice(1) : 'Turn';
            const timestamp = turn.timestamp_iso ? new Date(turn.timestamp_iso).toLocaleString() : '';
            const turnLabel = `## ${role} — turn ${turn.turn}`;
            lines.push(turnLabel);
            if (timestamp) {
                lines.push(`> ${timestamp}`);
            }
            lines.push('');

            let text = turn.original_text || turn.text || '';
            if (turn.role !== 'user') {
                text = Turns._stripAssistantMetadata(text);
            }
            text = String(text || '').trimEnd();
            lines.push(text || '_No content_');
            lines.push('');
        });

        return lines.join('\n');
    },

    /**
     * Build a safe filename for markdown export
     */
    buildSessionMarkdownFilename(sessionId, session) {
        const rawName = session?.session_name || sessionId;
        const safeName = String(rawName || '')
            .replace(/[^a-z0-9-_]+/gi, '_')
            .replace(/^_+|_+$/g, '');
        const base = safeName || sessionId.substring(0, 12);
        return `session_${base}.md`;
    },

    /**
     * Download a text file in the browser
     */
    downloadTextFile(content, filename, mimeType = 'text/plain') {
        const blob = new Blob([content], { type: `${mimeType};charset=utf-8` });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    },

    /**
     * Load dashboard
     */
    async loadDashboard() {
        const containerId = 'extraction-tab-content';
        const dashboardId = 'extraction';
        const container = document.getElementById(containerId);
        
        try {
            // Try to use DashboardRenderer if available (wait up to 500ms)
            const renderer = await this.waitForRenderer(500);
            
            if (renderer) {
                await renderer.renderDeclarativeDashboard(containerId, this.workspaceId, dashboardId);
            } else {
                // ES6 module not loaded - fetch data directly and render basic metrics
                const data = await this.fetchDashboardData(dashboardId);
                this.renderBasicDashboard(container, data);
            }
        } catch (error) {
            console.error('Failed to load dashboard:', error);
            if (container) {
                container.innerHTML = `<div class="text-center py-12 text-red-500 font-mono">Failed to load dashboard</div>`;
            }
        }
    },
    
    /**
     * Wait for DashboardRenderer module
     */
    waitForRenderer(maxWaitMs) {
        if (window.DashboardRenderer) return Promise.resolve(window.DashboardRenderer);
        
        return new Promise(resolve => {
            const start = Date.now();
            const check = () => {
                if (window.DashboardRenderer) return resolve(window.DashboardRenderer);
                if (Date.now() - start >= maxWaitMs) return resolve(null);
                setTimeout(check, 25);
            };
            check();
        });
    },
    
    /**
     * Fetch dashboard data from API
     */
    async fetchDashboardData(dashboardId) {
        const response = await fetch(`/api/browse/workspace/${this.workspaceId}/dashboards/${dashboardId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
    },
    
    /**
     * Render basic dashboard (fallback when ES6 module unavailable)
     */
    renderBasicDashboard(container, data) {
        if (!data.is_available) {
            container.innerHTML = `<div class="text-center py-12 text-terminal-gray font-mono">${data.message || 'Dashboard not available'}</div>`;
            return;
        }
        
        const metrics = data.data?.metrics || {};
        const config = data.config || {};
        
        let html = '<div class="metrics-grid mb-6">';
        for (const m of (config.metrics || [])) {
            const val = metrics[m.id];
            html += `<div class="metric-card"><p class="metric-label">${m.title}</p><div class="metric-value">${val ?? '-'}</div></div>`;
        }
        html += '</div><p class="text-terminal-gray text-sm font-mono text-center">Charts require a browser with ES6 module support</p>';
        container.innerHTML = html;
    },

    /**
     * Show dashboard view
     */
    showDashboard(skipHistory = false) {
        this.currentView = 'dashboard';
        document.getElementById('dashboard-view').classList.remove('hidden');
        document.getElementById('session-view').classList.add('hidden');
        
        document.getElementById('dashboard-link').classList.add('active');
        document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
        
        if (!skipHistory) {
            this.updateHistory();
        }
    },

    /**
     * Show session view
     */
    showSessionView(skipHistory = false) {
        this.currentView = 'session';
        document.getElementById('dashboard-view').classList.add('hidden');
        document.getElementById('session-view').classList.remove('hidden');
        
        document.getElementById('dashboard-link').classList.remove('active');
        
        if (!skipHistory) {
            this.updateHistory();
        }
    },

    /**
     * Update status badge
     */
    updateStatusBadge() {
        // Status updates handled by extraction flow
    },

    /**
     * Update action bars
     */
    updateActionBars() {
        setTimeout(() => this.updateContentHeight(), 0);
    },

    /**
     * Show extraction status panel
     */
    showExtractionStatus(type, title, subtitle, actions = []) {
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('main-content').classList.add('hidden');
        document.getElementById('extraction-status').classList.remove('hidden');
        
        const icons = { error: '❌', success: '✅', info: 'ℹ️', loading: '⏳' };
        
        document.getElementById('status-icon').textContent = icons[type] || '❓';
        document.getElementById('status-title').textContent = title;
        document.getElementById('status-subtitle').textContent = subtitle;
        
        const actionsEl = document.getElementById('status-actions');
        actionsEl.innerHTML = actions.map(action => `
            <button onclick="${action.onclick}" 
                    class="px-4 py-2 ${action.secondary ? 'bg-surface-dark text-white hover:bg-border-dark' : 'bg-primary text-white hover:bg-primary-dark'} rounded text-sm font-mono">
                ${action.label}
            </button>
        `).join('');
    },

    /**
     * Show loading state
     */
    showLoading(message = 'Loading...') {
        document.getElementById('loading').classList.remove('hidden');
        document.getElementById('main-content').classList.add('hidden');
        document.getElementById('extraction-status').classList.add('hidden');
        document.getElementById('loading-message').textContent = message;
    },

    /**
     * Show main content
     */
    showMainContent() {
        document.getElementById('loading').classList.add('hidden');
        document.getElementById('main-content').classList.remove('hidden');
        document.getElementById('extraction-status').classList.add('hidden');
        this.updateContentHeight();
    }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', () => WorkspacePage.init());

// Global functions for onclick handlers (backward compatibility)
window.selectSession = (id) => WorkspacePage.selectSession(id);
window.filterSessions = (term) => WorkspacePage.filterSessions(term);
window.clearSessionSearch = () => WorkspacePage.clearSessionSearch();
window.filterTurns = (term) => WorkspacePage.filterTurns(term);
window.clearTurnSearch = () => WorkspacePage.clearTurnSearch();
window.showDashboard = () => WorkspacePage.showDashboard();
window.refreshExtraction = () => WorkspacePage.refreshExtraction();
window.reextractWorkspace = () => WorkspacePage.reextractWorkspace();
window.syncWorkspace = () => WorkspacePage.syncWorkspace();
window.closeExtractionModal = () => WorkspacePage.closeExtractionModal();

// Toggle functions for backward compatibility
window.toggleFilesList = (id) => Turns.toggleFilesList(id);
window.toggleCodeEdits = (id) => Turns.toggleCodeEdits(id);
window.toggleToolsList = (id, e) => Turns.toggleToolsList(id, e);

// Export for use in other modules
window.WorkspacePage = WorkspacePage;
