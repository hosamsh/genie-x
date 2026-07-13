/**
 * Agent Info - Dynamic agent display configuration loaded from server
 */

const AgentInfo = {
    agents: {},
    loading: false,
    loaded: false,

    /**
     * Initialize and load agent metadata from server
     */
    async init() {
        if (this.loaded || this.loading) return;
        
        this.loading = true;
        
        try {
            const response = await fetch('/api/agents/');
            if (!response.ok) {
                throw new Error(`Failed to load agents: ${response.statusText}`);
            }
            
            const data = await response.json();
            
            // Build agents map from server data
            this.agents = {};
            for (const agent of data.agents) {
                const versionedLogo = agent.icon
                    ? `/api/agents/${agent.id}/icon?v=${encodeURIComponent(agent.icon)}`
                    : `/api/agents/${agent.id}/icon`;
                this.agents[agent.id] = {
                    logo: agent.icon_url || versionedLogo,
                    name: agent.display_name || agent.name,
                    color: agent.color || 'bg-surface-dark text-terminal-gray',
                };
            }
            
            // Add default agent
            this.agents['default'] = {
                logo: '/api/agents/default/icon',
                name: 'Agent',
                color: 'bg-surface-dark text-terminal-gray'
            };
            
            this.loaded = true;
        } catch (error) {
            console.error('Failed to load agent metadata:', error);
            // Fallback to default
            this.agents = {
                'default': {
                    logo: '/static/img/agent-logo.svg',
                    name: 'Agent',
                    color: 'bg-surface-dark text-terminal-gray'
                }
            };
            this.loaded = true;
        } finally {
            this.loading = false;
        }
    },

    /**
     * Get agent display info
     */
    get(agent) {
        if (!this.loaded) {
            console.warn('AgentInfo not loaded yet, using default');
            return this.agents['default'] || {
                logo: '/static/img/agent-logo.svg',
                name: 'Agent',
                color: 'bg-surface-dark text-terminal-gray'
            };
        }
        return this.agents[agent] || this.agents['default'];
    },

    /**
     * Render agent badge HTML
     */
    renderBadge(agent) {
        const info = this.get(agent);
        return `<span class="px-2 py-1 text-xs rounded-full ${info.color} flex items-center gap-1 inline-flex">
            <img src="${info.logo}" alt="${info.name}" class="w-3 h-3 object-contain" onerror="this.src='/static/img/agent-logo.svg'"> 
            ${info.name}
        </span>`;
    },

    /**
     * Render agent logo only
     */
    renderLogo(agent, size = 3) {
        const info = this.get(agent);
        return `<img src="${info.logo}" alt="${info.name}" class="w-${size} h-${size} object-contain" onerror="this.src='/static/img/agent-logo.svg'">`;
    }
};

// Auto-initialize when script loads
AgentInfo.init();

// Export for use in other modules
window.AgentInfo = AgentInfo;
