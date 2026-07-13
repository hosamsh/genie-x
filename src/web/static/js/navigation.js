/**
 * Navigation State Manager
 * Handles workspace expansion, active states, and session loading
 */

class NavigationManager {
    constructor() {
        this.activeWorkspace = null;
        this.expandedWorkspaces = new Set();
        this.workspaces = [];
        this.sessions = new Map(); // workspace_id -> sessions array
        
        this.init();
    }
    
    async init() {
        this.setupEventListeners();

        if (!document.getElementById('nav-workspaces')) {
            return;
        }

        await this.loadWorkspaces();
        this.restoreState();
    }
    
    async loadWorkspaces() {
        try {
            const response = await fetch('/api/browse/workspaces?limit=100&include_live=true');
            const data = await response.json();
            this.workspaces = data.workspaces || [];
            this.renderWorkspaces();
        } catch (error) {
            console.error('Failed to load workspaces:', error);
        }
    }
    
    renderWorkspaces() {
        const container = document.getElementById('nav-workspaces');
        if (!container) return;
        
        // Only render workspace list on browse page, keep it as a simple link elsewhere
        if (window.location.pathname !== '/browse') {
            return;
        }
        
        if (this.workspaces.length === 0) {
            container.innerHTML = `
                <div class="nav-item" style="opacity: 0.5; cursor: default;">
                    <span class="material-symbols-outlined">folder_off</span>
                    <span>No workspaces</span>
                </div>
            `;
            return;
        }
        
        const html = this.workspaces.map(ws => {
            const isExpanded = this.expandedWorkspaces.has(ws.workspace_id);
            const isActive = this.activeWorkspace === ws.workspace_id;
            const sessions = this.sessions.get(ws.workspace_id) || [];
            
            return `
                <div class="nav-item-expandable ${isExpanded ? 'expanded' : ''}" data-workspace-id="${ws.workspace_id}">
                    <a class="nav-item ${isActive ? 'active' : ''}" 
                       href="/workspace/${ws.workspace_id}"
                       onclick="navManager.toggleWorkspace('${ws.workspace_id}', event)">
                        <span class="material-symbols-outlined">folder</span>
                        <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${this.truncate(ws.workspace_name || ws.workspace_id, 20)}</span>
                        <span class="nav-item-toggle material-symbols-outlined">expand_more</span>
                    </a>
                    <div class="nav-subitems">
                        ${sessions.length === 0 ? `
                            <div class="nav-subitem" style="opacity: 0.5; cursor: default;">
                                <span class="material-symbols-outlined" style="font-size: 0.875rem;">info</span>
                                <span>Loading...</span>
                            </div>
                        ` : sessions.map(session => `
                            <a class="nav-subitem" 
                               href="/workspace/${ws.workspace_id}?session=${session.session_id}"
                               data-session-id="${session.session_id}">
                                <span class="material-symbols-outlined" style="font-size: 0.875rem;">chat</span>
                                <span style="flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${this.truncate(session.title || `Session ${session.session_id.substring(0, 8)}`, 18)}</span>
                            </a>
                        `).join('')}
                    </div>
                </div>
            `;
        }).join('');
        
        container.innerHTML = html;
    }
    
    async toggleWorkspace(workspaceId, event) {
        if (event) {
            // Only prevent default if clicking the toggle icon area
            const isToggle = event.target.classList.contains('nav-item-toggle') || 
                           event.target.closest('.nav-item-toggle');
            if (isToggle) {
                event.preventDefault();
                event.stopPropagation();
            }
        }
        
        if (this.expandedWorkspaces.has(workspaceId)) {
            this.expandedWorkspaces.delete(workspaceId);
        } else {
            this.expandedWorkspaces.add(workspaceId);
            // Load sessions if not already loaded
            if (!this.sessions.has(workspaceId)) {
                await this.loadWorkspaceSessions(workspaceId);
            }
        }
        
        this.renderWorkspaces();
        this.saveState();
    }
    
    async loadWorkspaceSessions(workspaceId) {
        try {
            const response = await fetch(`/api/browse/workspace/${workspaceId}/sessions`);
            const data = await response.json();
            this.sessions.set(workspaceId, data.sessions || []);
            this.renderWorkspaces();
        } catch (error) {
            console.error(`Failed to load sessions for workspace ${workspaceId}:`, error);
            this.sessions.set(workspaceId, []);
        }
    }
    
    setActiveWorkspace(workspaceId) {
        this.activeWorkspace = workspaceId;
        if (workspaceId && !this.expandedWorkspaces.has(workspaceId)) {
            this.expandedWorkspaces.add(workspaceId);
            if (!this.sessions.has(workspaceId)) {
                this.loadWorkspaceSessions(workspaceId);
            }
        }
        this.renderWorkspaces();
        this.saveState();
    }
    
    setActiveSession(sessionId) {
        // Update active session styling
        document.querySelectorAll('.nav-subitem').forEach(item => {
            if (item.dataset.sessionId === sessionId) {
                item.classList.add('active');
            } else {
                item.classList.remove('active');
            }
        });
    }
    
    setupEventListeners() {
        // Mobile menu toggle
        const menuBtn = document.getElementById('mobile-menu-btn');
        const sidebar = document.querySelector('.nav-sidebar');
        
        if (menuBtn && sidebar) {
            menuBtn.addEventListener('click', () => {
                sidebar.classList.toggle('mobile-open');
            });
            
            // Close on outside click
            document.addEventListener('click', (e) => {
                if (!sidebar.contains(e.target) && !menuBtn.contains(e.target)) {
                    sidebar.classList.remove('mobile-open');
                }
            });
        }
    }
    
    saveState() {
        const state = {
            activeWorkspace: this.activeWorkspace,
            expandedWorkspaces: Array.from(this.expandedWorkspaces)
        };
        localStorage.setItem('nav-state', JSON.stringify(state));
    }
    
    restoreState() {
        try {
            const saved = localStorage.getItem('nav-state');
            if (saved) {
                const state = JSON.parse(saved);
                this.activeWorkspace = state.activeWorkspace;
                this.expandedWorkspaces = new Set(state.expandedWorkspaces || []);
                
                // Load sessions for expanded workspaces
                this.expandedWorkspaces.forEach(wsId => {
                    if (!this.sessions.has(wsId)) {
                        this.loadWorkspaceSessions(wsId);
                    }
                });
                
                this.renderWorkspaces();
            }
        } catch (error) {
            console.error('Failed to restore navigation state:', error);
        }
    }
    
    truncate(str, maxLen) {
        if (str.length <= maxLen) return str;
        return str.substring(0, maxLen - 1) + '…';
    }
    
    refresh() {
        this.loadWorkspaces();
    }
}

// Sidebar Resizer - Standalone functionality
function initSidebarResizer() {
    const resizer = document.getElementById('sidebar-resizer');
    if (!resizer) return;
    
    let isResizing = false;
    let startX = 0;
    let startWidth = 0;
    
    // Load saved width
    const savedWidth = localStorage.getItem('sidebar-width');
    if (savedWidth) {
        setSidebarWidth(parseInt(savedWidth));
    }
    
    resizer.addEventListener('mousedown', (e) => {
        isResizing = true;
        startX = e.clientX;
        startWidth = document.querySelector('.nav-sidebar').offsetWidth;
        document.body.classList.add('resizing-sidebar');
        resizer.classList.add('resizing');
        e.preventDefault();
    });
    
    document.addEventListener('mousemove', (e) => {
        if (!isResizing) return;
        
        const delta = e.clientX - startX;
        const newWidth = Math.max(200, Math.min(500, startWidth + delta));
        setSidebarWidth(newWidth);
    });
    
    document.addEventListener('mouseup', () => {
        if (isResizing) {
            isResizing = false;
            document.body.classList.remove('resizing-sidebar');
            resizer.classList.remove('resizing');
            
            // Save width
            const width = document.querySelector('.nav-sidebar').offsetWidth;
            localStorage.setItem('sidebar-width', width);
        }
    });
}

function setSidebarWidth(width) {
    document.documentElement.style.setProperty('--sidebar-width', `${width}px`);
}

// Global instance
let navManager;

/**
 * Detect and display the username from the browser/system
 * Uses the system username if available, falls back to other methods
 */
function initializeUserDisplay() {
    const userAvatarEl = document.getElementById('user-avatar');
    const userNameEl = document.getElementById('user-name');
    
    if (!userAvatarEl || !userNameEl) return;
    
    // Try to get username from environment (Windows sets this in some contexts)
    // or use a stored preference
    let username = localStorage.getItem('chat-explorer-username');
    
    if (!username) {
        // Try to extract from common patterns
        // On Windows, we can sometimes detect via path patterns or stored data
        const storedUser = sessionStorage.getItem('detected-user');
        if (storedUser) {
            username = storedUser;
        } else {
            // Fallback: use a generic but friendly name based on the machine
            // We'll show a placeholder and the user can customize it
            username = navigator.userAgent.includes('Windows') ? 'User' : 'user';
            
            // Try to detect from any workspace paths we might have cached
            const navState = localStorage.getItem('nav-state');
            if (navState) {
                try {
                    // Often workspace paths contain the username
                    const parsed = JSON.parse(navState);
                    // Will be populated later from workspace data
                } catch (e) {}
            }
        }
    }
    
    // Display the username
    const displayName = username || 'User';
    const initials = displayName.substring(0, 2).toUpperCase();
    
    userAvatarEl.textContent = initials;
    userNameEl.textContent = displayName;
    
    // Make it clickable to set custom name
    userNameEl.style.cursor = 'pointer';
    userNameEl.title = 'Click to set your name';
    userNameEl.addEventListener('click', () => {
        const newName = prompt('Enter your display name:', displayName);
        if (newName && newName.trim()) {
            localStorage.setItem('chat-explorer-username', newName.trim());
            userNameEl.textContent = newName.trim();
            userAvatarEl.textContent = newName.trim().substring(0, 2).toUpperCase();
        }
    });
}

// Initialize on DOM ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        initSidebarResizer();
        navManager = new NavigationManager();
        initializeUserDisplay();
    });
} else {
    initSidebarResizer();
    navManager = new NavigationManager();
    initializeUserDisplay();
}
