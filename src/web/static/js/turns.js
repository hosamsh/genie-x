/**
 * Turns - Turn rendering and filtering
 */

const Turns = {
    list: [],
    searchTerm: '',
    areAllCollapsed: false,
    collapsedTextMaxHeightClass: 'max-h-64',

    /**
     * Strip assistant-only injected metadata lines like:
     * [cursor metadata: bubble_id=...]
     * [(agent name) metadata: ...]
     */
    _stripAssistantMetadata(text) {
        if (!text) return '';

        // Fast-path: avoid split/join when there's clearly nothing to do
        if (text.indexOf('metadata:') === -1 && text.indexOf('metadata :') === -1) {
            return text;
        }

        const lines = text.split('\n');
        const kept = [];

        for (const line of lines) {
            const trimmed = line.trim();

            // Match a whole-line bracketed metadata tag.
            // Examples:
            //   [cursor metadata: bubble_id=...]
            //   [(agent name) metadata: ...]
            if (/^\[\s*[^\]]*\bmetadata\s*:\s*[^\]]*\]\s*$/i.test(trimmed)) {
                continue;
            }

            kept.push(line);
        }

        return kept.join('\n');
    },

    _getTurnById(turnId) {
        return this.list.find(turn => turn.turn === turnId);
    },

    _subagentRunsByToolCallId(turn) {
        const mapping = new Map();
        const subagentRuns = Array.isArray(turn.subagent_runs) ? turn.subagent_runs : [];
        subagentRuns.forEach(subagentRun => {
            const callId = subagentRun.source_tool_call_id || '';
            if (!callId) return;
            if (!mapping.has(callId)) mapping.set(callId, []);
            mapping.get(callId).push(subagentRun);
        });
        return mapping;
    },

    _buildInlineToolRef(turnId, toolRun) {
        return `<button type="button" onclick="Turns.showToolRunDetails(${turnId}, ${toolRun.tool_index}); return false;" class="inline-flex items-center gap-1 px-1.5 py-0.5 ml-1 rounded border border-primary/30 bg-primary/10 text-[11px] font-mono text-primary hover:bg-primary/20 hover:text-white">tool ${toolRun.tool_index + 1}</button>`;
    },

    _buildInlineSubagentRef(turnId, subagentRun) {
        return `<button type="button" onclick="Turns.showSubagentDetails(${turnId}, ${subagentRun.subagent_index}); return false;" class="inline-flex items-center gap-1 px-1.5 py-0.5 ml-1 rounded border border-terminal-green/30 bg-terminal-green/10 text-[11px] font-mono text-terminal-green hover:bg-terminal-green/20 hover:text-white">agent ${subagentRun.subagent_index + 1}</button>`;
    },

    _decorateAssistantText(displayText, turn) {
        const toolRuns = Array.isArray(turn.tool_runs) ? turn.tool_runs : [];
        if (toolRuns.length === 0) {
            return displayText;
        }

        const subagentsByCallId = this._subagentRunsByToolCallId(turn);
        let annotatedText = displayText;
        const deferredRefs = [];

        toolRuns.forEach(toolRun => {
            const inlineParts = [this._buildInlineToolRef(turn.turn, toolRun)];
            const subagents = subagentsByCallId.get(toolRun.call_id || '') || [];
            subagents.forEach(subagentRun => {
                inlineParts.push(this._buildInlineSubagentRef(turn.turn, subagentRun));
            });
            const inlineRefHtml = inlineParts.join('');
            const matchText = toolRun.display_text || '';
            if (matchText && annotatedText.includes(matchText)) {
                annotatedText = annotatedText.replace(matchText, `${matchText} ${inlineRefHtml}`);
            } else {
                deferredRefs.push(`${Formatters.escapeHtml(toolRun.name || `tool ${toolRun.tool_index + 1}`)} ${inlineRefHtml}`);
            }
        });

        if (deferredRefs.length > 0) {
            annotatedText += `\n\n<div class="mt-3 flex flex-wrap gap-2">${deferredRefs.join('')}</div>`;
        }
        return annotatedText;
    },

    _ensureDetailModal() {
        let modal = document.getElementById('turn-detail-modal');
        if (modal) return modal;

        modal = document.createElement('div');
        modal.id = 'turn-detail-modal';
        modal.className = 'hidden modal-overlay';
        modal.innerHTML = `
            <div class="modal-content max-w-5xl" style="background: var(--surface-dark); border: 1px solid var(--border-dark);">
                <div class="flex items-center justify-between p-4 border-b border-border-dark">
                    <div>
                        <h2 id="turn-detail-modal-title" class="text-lg font-semibold text-white font-mono">Details</h2>
                        <p id="turn-detail-modal-subtitle" class="text-xs text-terminal-gray font-mono mt-1"></p>
                    </div>
                    <button type="button" onclick="Turns.closeDetailModal()" class="btn btn-secondary text-xs py-1.5 px-3">CLOSE</button>
                </div>
                <div id="turn-detail-modal-body" class="p-4 max-h-[75vh] overflow-y-auto"></div>
            </div>
        `;
        modal.addEventListener('click', (event) => {
            if (event.target === modal) {
                this.closeDetailModal();
            }
        });
        document.body.appendChild(modal);
        return modal;
    },

    showDetailModal(title, subtitle, bodyHtml) {
        const modal = this._ensureDetailModal();
        document.getElementById('turn-detail-modal-title').textContent = title;
        document.getElementById('turn-detail-modal-subtitle').textContent = subtitle || '';
        document.getElementById('turn-detail-modal-body').innerHTML = bodyHtml;
        modal.classList.remove('hidden');
    },

    closeDetailModal() {
        const modal = document.getElementById('turn-detail-modal');
        if (modal) modal.classList.add('hidden');
    },

    _renderJsonBlock(label, value) {
        const payload = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
        return `
            <div class="mb-4">
                <div class="text-xs uppercase tracking-wide text-terminal-gray font-mono mb-2">${Formatters.escapeHtml(label)}</div>
                <pre class="bg-background-dark border border-border-dark rounded p-3 text-xs text-white font-mono overflow-x-auto whitespace-pre-wrap">${Formatters.escapeHtml(payload || '')}</pre>
            </div>
        `;
    },

    showToolRunDetails(turnId, toolIndex) {
        const turn = this._getTurnById(turnId);
        const toolRun = turn?.tool_runs?.find(item => item.tool_index === toolIndex);
        if (!turn || !toolRun) return;

        const summary = [
            toolRun.kind ? `kind: ${toolRun.kind}` : '',
            toolRun.status ? `status: ${toolRun.status}` : '',
            toolRun.spawned_session_id ? `spawned: ${toolRun.spawned_session_id}` : '',
        ].filter(Boolean).join(' • ');

        let bodyHtml = `
            <div class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm font-mono">
                    <div class="dashboard-card"><div class="metric-label">Tool</div><div class="text-white">${Formatters.escapeHtml(toolRun.name || 'tool')}</div></div>
                    <div class="dashboard-card"><div class="metric-label">Status</div><div class="text-white">${Formatters.escapeHtml(toolRun.status || 'unknown')}</div></div>
                    <div class="dashboard-card"><div class="metric-label">Call ID</div><div class="text-white break-all">${Formatters.escapeHtml(toolRun.call_id || '-')}</div></div>
                </div>
        `;

        bodyHtml += this._renderJsonBlock('Arguments', toolRun.arguments || {});
        if (toolRun.results && toolRun.results.length > 0) {
            toolRun.results.forEach((result, index) => {
                bodyHtml += this._renderJsonBlock(`Result ${index + 1}`, result);
            });
        }
        bodyHtml += this._renderJsonBlock('Raw Call', toolRun.raw_call || {});
        if (toolRun.extra && Object.keys(toolRun.extra).length > 0) {
            bodyHtml += this._renderJsonBlock('Extra', toolRun.extra);
        }
        if (toolRun.spawned_session_id) {
            bodyHtml += `
                <div class="mt-4">
                    <button type="button" onclick="Turns.openSubagentSession('${toolRun.spawned_session_id}')" class="btn btn-primary text-xs py-1.5 px-3">
                        OPEN SPAWNED SUBAGENT
                    </button>
                </div>
            `;
        }
        bodyHtml += `</div>`;

        this.showDetailModal(`Tool Run: ${toolRun.name || 'tool'}`, summary, bodyHtml);
    },

    async showSubagentDetails(turnId, subagentIndex) {
        const turn = this._getTurnById(turnId);
        const subagentRun = turn?.subagent_runs?.find(item => item.subagent_index === subagentIndex);
        if (!turn || !subagentRun) return;

        this.showDetailModal(
            `Subagent: ${subagentRun.title || subagentRun.subagent_session_id}`,
            `${subagentRun.turn_count || 0} turns • +${subagentRun.total_lines_added || 0} / -${subagentRun.total_lines_removed || 0}`,
            `<div class="text-terminal-gray font-mono">Loading subagent transcript...</div>`,
        );

        let childTurns = [];
        try {
            const response = await fetch(`/api/browse/session/${subagentRun.subagent_session_id}/turns`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const payload = await response.json();
            childTurns = payload.turns || [];
        } catch (error) {
            childTurns = [];
        }

        const transcriptHtml = childTurns.length > 0
            ? childTurns.map(childTurn => {
                const role = childTurn.role === 'assistant' ? 'Assistant' : 'User';
                const text = childTurn.original_text || childTurn.text || '';
                return `
                    <div class="border border-border-dark rounded-lg p-3 bg-background-dark mb-3">
                        <div class="flex items-center justify-between mb-2 text-xs font-mono text-terminal-gray">
                            <span>${role} • turn ${childTurn.turn}</span>
                            <span>${childTurn.timestamp_iso ? new Date(childTurn.timestamp_iso).toLocaleString() : ''}</span>
                        </div>
                        <div class="markdown-content text-sm text-white">${Formatters.renderMarkdown(text)}</div>
                    </div>
                `;
            }).join('')
            : '<div class="text-terminal-gray font-mono">No child transcript available.</div>';

        const bodyHtml = `
            <div class="space-y-4">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm font-mono">
                    <div class="dashboard-card"><div class="metric-label">Turns</div><div class="text-white">${Formatters.number(subagentRun.turn_count || 0)}</div></div>
                    <div class="dashboard-card"><div class="metric-label">Lines Added</div><div class="text-white">${Formatters.number(subagentRun.total_lines_added || 0)}</div></div>
                    <div class="dashboard-card"><div class="metric-label">Lines Removed</div><div class="text-white">${Formatters.number(subagentRun.total_lines_removed || 0)}</div></div>
                </div>
                ${subagentRun.prompt_text ? `<div><div class="text-xs uppercase tracking-wide text-terminal-gray font-mono mb-2">Prompt</div><div class="bg-background-dark border border-border-dark rounded p-3 text-sm text-white markdown-content">${Formatters.renderMarkdown(subagentRun.prompt_text)}</div></div>` : ''}
                ${subagentRun.result_text ? `<div><div class="text-xs uppercase tracking-wide text-terminal-gray font-mono mb-2">Latest Output</div><div class="bg-background-dark border border-border-dark rounded p-3 text-sm text-white markdown-content">${Formatters.renderMarkdown(subagentRun.result_text)}</div></div>` : ''}
                <div class="flex gap-2">
                    <button type="button" onclick="Turns.openSubagentSession('${subagentRun.subagent_session_id}')" class="btn btn-primary text-xs py-1.5 px-3">OPEN FULL SESSION</button>
                </div>
                <div>
                    <div class="text-xs uppercase tracking-wide text-terminal-gray font-mono mb-2">Transcript</div>
                    ${transcriptHtml}
                </div>
            </div>
        `;

        this.showDetailModal(
            `Subagent: ${subagentRun.title || subagentRun.subagent_session_id}`,
            `${subagentRun.subagent_session_id}`,
            bodyHtml,
        );
    },

    openSubagentSession(subagentSessionId) {
        if (!window.WorkspacePage || !window.WorkspacePage.workspaceId) return;
        window.location.href = `/workspace/${window.WorkspacePage.workspaceId}/session/${subagentSessionId}`;
    },

    /**
     * Load turns for a session
     */
    async load(sessionId) {
        const response = await fetch(`/api/browse/session/${sessionId}/turns`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const data = await response.json();
        this.list = data.turns || [];
        return this.list;
    },

    /**
     * Filter turns by search term
     */
    filter(searchTerm) {
        this.searchTerm = (searchTerm || '').toLowerCase().trim();
        
        // Show/hide clear button
        const clearBtn = document.getElementById('turn-search-clear');
        if (clearBtn) {
            clearBtn.classList.toggle('hidden', !this.searchTerm);
        }
        
        if (this.list.length === 0) return [];
        
        // Filter turns
        const filtered = this.list.filter(turn => {
            if (!this.searchTerm) return true;

            let displayText = (turn.original_text || turn.text || '');
            if (turn.role !== 'user') {
                displayText = this._stripAssistantMetadata(displayText);
            }

            displayText = displayText.toLowerCase();
            return displayText.includes(this.searchTerm);
        });
        
        // Update count
        const countEl = document.getElementById('turn-count');
        if (countEl) {
            countEl.textContent = this.searchTerm 
                ? `${filtered.length}/${this.list.length} turns` 
                : `${this.list.length} turns`;
        }
        
        return filtered;
    },

    /**
     * Clear search
     */
    clearSearch() {
        this.searchTerm = '';
        const searchInput = document.getElementById('turn-search');
        if (searchInput) searchInput.value = '';
        
        const clearBtn = document.getElementById('turn-search-clear');
        if (clearBtn) clearBtn.classList.add('hidden');
        
        if (this.list.length > 0) {
            return this.filter('');
        }
        return [];
    },

    /**
     * Render turns list
     */
    render(container, turns) {
        if (!container) return;
        
        if (turns.length === 0) {
            container.innerHTML = `
                <div class="text-center text-terminal-gray py-8 font-mono">
                    ${this.searchTerm ? 'No matching turns' : 'No turns'}
                </div>
            `;
            return;
        }

        // If search is active, show flat list without groupings/dots
        if (this.searchTerm) {
            container.innerHTML = turns.map(turn => this._renderTurn(turn)).join('<div class="h-4"></div>');
            this._applyTurnTextClamps(container);
            return;
        }

        // Group turns into combined exchanges (User + following Assistant turns)
        const groups = [];
        let currentGroup = null;

        turns.forEach(turn => {
            if (turn.role === 'user') {
                if (currentGroup) groups.push(currentGroup);
                currentGroup = {
                    id: `exchange-${turn.turn}`,
                    userTurn: turn,
                    assistantTurns: []
                };
            } else {
                if (currentGroup) {
                    currentGroup.assistantTurns.push(turn);
                } else {
                    // Orphan assistant turn (e.g. start of chat or filtered context)
                    groups.push({
                        id: `exchange-${turn.turn}`,
                        userTurn: null,
                        assistantTurns: [turn]
                    });
                }
            }
        });
        if (currentGroup) groups.push(currentGroup);
        
        // Render groups
        const groupsHtml = groups.map(group => this._renderGroup(group)).join('');

        // Global toggle control (Two circles aligned with the timeline)
        // Made more visible with primary color and glow effect
        const toggleHtml = `
            <div class="turn-group flex gap-4 mb-2 relative">
                <div class="flex flex-col items-center cursor-pointer group p-2 -m-2 rounded-lg hover:bg-primary/10 transition-all duration-300" onclick="Turns.toggleAllGroups()" title="${this.areAllCollapsed ? 'Expand all' : 'Collapse all'}">
                    <div class="flex gap-1.5 items-center">
                         <div class="w-2.5 h-2.5 rounded-full bg-primary/70 group-hover:bg-primary group-hover:shadow-[0_0_8px_rgba(59,130,246,0.6)] transition-all duration-300"></div>
                         <div class="w-2.5 h-2.5 rounded-full bg-primary/70 group-hover:bg-primary group-hover:shadow-[0_0_8px_rgba(59,130,246,0.6)] transition-all duration-300"></div>
                    </div>
                </div>
            </div>
        `;

        container.innerHTML = toggleHtml + groupsHtml;

        // Clamp long turn bodies and show an expander only when needed
        this._applyTurnTextClamps(container);

        // If a turn hash is present, scroll to and highlight it
        // Delay to ensure layout is stable after clamps are applied (clamps have a 100ms delay)
        setTimeout(() => this._scrollToHash(container), 150);
    },

    /**
     * Render a group of turns (User + Assistant)
     */
    _renderGroup(group) {
        const content = [];
        if (group.userTurn) content.push(this._renderTurn(group.userTurn));
        group.assistantTurns.forEach(t => content.push(this._renderTurn(t)));
        
        // Add spacing between bubbles within the group
        const joinedContent = content.join('<div class="h-4"></div>'); 

        // Summaries
        const userText = group.userTurn 
            ? (group.userTurn.original_text || group.userTurn.text || '')
            : '';
        const userSummary = userText ? (userText.slice(0, 80).replace(/\n/g, ' ') + (userText.length > 80 ? '...' : '')) : 'User message';

        let assistantSummary = '';
        if (group.assistantTurns.length > 0) {
             const t = group.assistantTurns.find(turn => {
                 let txt = turn.original_text || turn.text || '';
                 txt = this._stripAssistantMetadata(txt).trim();
                 return txt.length > 0;
             }) || group.assistantTurns[0];

             let txt = t.original_text || t.text || '';
             txt = this._stripAssistantMetadata(txt);
             if (!txt.trim()) {
                 if (t.tools && t.tools.length > 0) txt = `[Used tool: ${t.tools[0]}]`;
                 else if (t.files && t.files.length > 0) txt = `[ Referenced files ]`;
                 else txt = 'Assistant message';
             }
             
             assistantSummary = txt.slice(0, 80).replace(/\n/g, ' ') + (txt.length > 80 ? '...' : '');
        }

        return `
        <div class="turn-group flex gap-4 mb-8 relative" id="${group.id}">
            <!-- Connection Line / Collapse Trigger - Made more visible -->
            <div class="flex flex-col items-center pt-5 pb-5 cursor-pointer group/line px-1 -mx-1 rounded hover:bg-primary/5 transition-all duration-200" onclick="Turns.toggleGroup('${group.id}')" title="Collapse/Expand turn group">
                <div class="w-3 h-3 rounded-full bg-primary/60 group-hover/line:bg-primary group-hover/line:shadow-[0_0_10px_rgba(59,130,246,0.5)] transition-all duration-200"></div>
                ${group.assistantTurns.length > 0 ? `
                    <div class="w-1 flex-grow bg-primary/30 group-hover/line:bg-primary/70 transition-all duration-200 my-1 mb-1 rounded-full"></div>
                    <div class="w-3 h-3 rounded-full bg-primary/60 group-hover/line:bg-primary group-hover/line:shadow-[0_0_10px_rgba(59,130,246,0.5)] transition-all duration-200"></div>
                ` : `
                    <div class="w-1 flex-grow bg-primary/30 group-hover/line:bg-primary/70 transition-all duration-200 my-1 mb-1 rounded-full"></div>
                `}
            </div>

            <!-- Expanded Content -->
            <div class="flex-1 min-w-0 group-content transition-all duration-300" id="${group.id}-content">
                ${joinedContent}
            </div>

            <!-- Collapsed State (Hidden by default) -->
            <div class="hidden flex-1 flex flex-col justify-between py-4 px-4 bg-surface-dark/30 border border-border-dark rounded-lg cursor-pointer hover:bg-surface-dark transition-colors gap-6" 
                 id="${group.id}-collapsed"
                 onclick="Turns.toggleGroup('${group.id}')">
                 
                 ${group.userTurn ? `
                 <div class="flex items-center gap-3">
                     <span class="text-lg leading-none">👤</span>
                     <span class="text-terminal-gray font-mono text-sm truncate opacity-80">${Formatters.escapeHtml(userSummary)}</span>
                 </div>
                 ` : ''}

                 ${assistantSummary ? `
                 <div class="flex items-center gap-3">
                     <span class="text-lg leading-none">🤖</span>
                     <span class="text-terminal-gray font-mono text-sm truncate opacity-80">${Formatters.escapeHtml(assistantSummary)}</span>
                 </div>
                 ` : ''}
            </div>
        </div>
        `;
    },

    /**
     * Toggle group collapse state
     */
    toggleGroup(groupId) {
        const content = document.getElementById(`${groupId}-content`);
        const collapsed = document.getElementById(`${groupId}-collapsed`);
        const groupEl = document.getElementById(groupId);
        
        if (!content || !collapsed) return;

        if (content.classList.contains('hidden')) {
            // Expand
            content.classList.remove('hidden');
            collapsed.classList.add('hidden');
        } else {
            // Collapse
            content.classList.add('hidden');
            collapsed.classList.remove('hidden');
        }
    },

    _scrollToHash(container) {
        if (!window.location.hash) return;
        const targetId = window.location.hash.substring(1);
        if (!targetId.startsWith('turn-')) return;

        // Use double requestAnimationFrame to ensure DOM is fully laid out
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                const el = document.getElementById(targetId);
                if (el) {
                    // 1. Ensure the parent group is expanded
                    const group = el.closest('.turn-group');
                    if (group) {
                        const content = group.querySelector('.group-content');
                        if (content && content.classList.contains('hidden')) {
                            this.toggleGroup(group.id);
                        }
                    }

                    // 2. Ensure the turn text itself is expanded if it was clamped
                    this.expandTurn(targetId);
                    
                    // 3. Scroll into view after a brief delay for any expansion animations
                    setTimeout(() => {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.classList.add('highlight-turn');
                        setTimeout(() => el.classList.remove('highlight-turn'), 3000);
                    }, 50);
                }
            });
        });
    },

    /**
     * Clamp long turn bodies and show an expander only when needed
     */
    _applyTurnTextClamps(container) {
        // Check once layout is likely stable.
        const checkClamps = () => {
            // Skip if container is not visible (scrollHeight will be 0)
            if (container.offsetParent === null) return;
            
            const wrappers = container.querySelectorAll('[data-turn-text-wrapper]');
            wrappers.forEach(wrapper => {
                const content = wrapper.querySelector('[data-turn-text-content]');
                const expander = wrapper.querySelector('[data-turn-text-expander]');
                if (!content || !expander) return;

                // Reset and measure
                wrapper.classList.remove(this.collapsedTextMaxHeightClass, 'overflow-hidden');
                expander.classList.add('hidden');

                const fullHeight = content.scrollHeight;
                
                // If it exceeds our threshold (~256px), apply the clamp
                if (fullHeight > 260) {
                    wrapper.classList.add(this.collapsedTextMaxHeightClass, 'overflow-hidden');
                    expander.classList.remove('hidden');
                }
            });
        };

        // Run immediately and after a short delay to catch layout shifts
        checkClamps();
        setTimeout(checkClamps, 100);
        
        // Also re-check if user resizes window
        if (!this._resizeHandlerAttached) {
            window.addEventListener('resize', () => {
                const currentContainer = document.getElementById('turns-container');
                if (currentContainer) this._applyTurnTextClamps(currentContainer);
            });
            this._resizeHandlerAttached = true;
        }
    },

    expandTurn(turnAnchorId, event) {
        if (event) event.stopPropagation();
        const bubble = document.getElementById(turnAnchorId);
        if (!bubble) return;

        const wrapper = bubble.querySelector('[data-turn-text-wrapper]');
        const expander = bubble.querySelector('[data-turn-text-expander]');
        if (!wrapper || !expander) return;

        wrapper.classList.remove(this.collapsedTextMaxHeightClass, 'overflow-hidden');
        expander.classList.add('hidden');
    },

    /**
     * Copy link to specific turn
     */
    copyTurnLink(turnId, btn) {
        const url = `${window.location.origin}${window.location.pathname}#turn-${turnId}`;
        navigator.clipboard.writeText(url).then(() => {
            this._showCopyFeedback(btn);
        });
    },

    /**
     * Copy turn text content
     */
    copyTurnText(turnId, btn) {
        const turn = this.list.find(t => t.turn === turnId);
        if (!turn) return;
        
        let text = turn.original_text || turn.text || '';
        if (turn.role !== 'user') {
            text = this._stripAssistantMetadata(text);
        }
        
        navigator.clipboard.writeText(text).then(() => {
             this._showCopyFeedback(btn);
        });
    },

    _showCopyFeedback(btn) {
        const span = btn.querySelector('span');
        const originalText = span.textContent;
        span.textContent = 'check';
        span.classList.add('text-terminal-green');
        setTimeout(() => {
            span.textContent = originalText;
            span.classList.remove('text-terminal-green');
        }, 1500);
    },

    /**
     * Render a single turn
     */
    _renderTurn(turn) {
        const isUser = turn.role === 'user';
        const bgColor = isUser ? 'bg-primary/10 border-primary/30' : 'bg-surface-dark border-border-dark';
        const alignment = isUser ? 'turn-user' : 'turn-assistant';
        const roleLabel = isUser ? 'You' : 'Assistant';
        const roleIcon = isUser ? '👤' : '🤖';
        const turnAnchorId = `turn-${turn.turn}`;
        const fadeFrom = isUser ? 'from-background-dark' : 'from-surface-dark';
        
        let displayText = turn.original_text || turn.text || '';
        if (!isUser) {
            displayText = this._stripAssistantMetadata(displayText);
            displayText = this._decorateAssistantText(displayText, turn);
        }
        const footerHtml = this._renderTurnFooter(turn, isUser);
        
        return `
            <div id="${turnAnchorId}" class="turn-bubble ${alignment} ${bgColor} border rounded-lg p-4 scroll-mt-6" data-turn-id="${turn.turn}">
                <div class="flex items-center gap-2 mb-2">
                    <span>${roleIcon}</span>
                    <span class="font-medium text-sm text-white">${roleLabel}</span>
                    <a href="#${turnAnchorId}" class="text-xs text-terminal-gray font-mono hover:text-white" title="Link to this turn">Turn ${turn.turn}</a>
                    
                    <button onclick="Turns.copyTurnLink(${turn.turn}, this)" class="text-terminal-gray hover:text-white transition-colors p-1 rounded hover:bg-white/5" title="Copy link to turn">
                        <span class="material-symbols-outlined text-[16px]">link</span>
                    </button>
                    <button onclick="Turns.copyTurnText(${turn.turn}, this)" class="text-terminal-gray hover:text-white transition-colors p-1 rounded hover:bg-white/5" title="Copy turn text">
                        <span class="material-symbols-outlined text-[16px]">content_copy</span>
                    </button>
                </div>
                <div class="relative" data-turn-text-wrapper>
                    <div class="text-sm text-white markdown-content" data-turn-text-content>${Formatters.renderMarkdown(displayText, this.searchTerm)}</div>
                    <div class="absolute inset-x-0 bottom-0 h-32 flex items-end justify-center bg-gradient-to-t ${fadeFrom} via-${isUser ? 'background-dark' : 'surface-dark'}/90 to-transparent rounded-b-lg pb-4" 
                         data-turn-text-expander>
                        <button onclick="Turns.expandTurn('${turnAnchorId}', event)" class="flex items-center gap-2 px-4 py-1.5 bg-surface-dark border border-primary/30 rounded-full text-xs font-medium text-primary hover:bg-primary/10 hover:text-white hover:border-primary/50 transition-all shadow-lg backdrop-blur-md group cursor-pointer z-10">
                             <span>Show more</span>
                             <span class="material-symbols-outlined text-sm group-hover:translate-y-0.5 transition-transform">expand_more</span>
                        </button>
                    </div>
                </div>
                ${turn.files && turn.files.length > 0 ? this._renderFilesList(turn) : ''}
                ${!isUser && ((turn.tool_runs && turn.tool_runs.length > 0) || (turn.tools && turn.tools.length > 0)) ? this._renderToolsList(turn) : ''}
                ${!isUser && turn.subagent_runs && turn.subagent_runs.length > 0 ? this._renderSubagentsList(turn) : ''}
                ${!isUser ? this._renderCodeEdits(turn) : ''}
                ${footerHtml}
            </div>
        `;
    },

    /**
     * Render turn footer with metrics
     */
    _renderTurnFooter(turn, isUser) {
        const parts = [];
        
        // Timestamp
        if (turn.timestamp_iso) {
            const date = new Date(turn.timestamp_iso);
            parts.push(`<span class="text-xs text-terminal-gray font-mono">${date.toLocaleTimeString()}</span>`);
        }
        
        // Visible tokens
        if (turn.total_visible_tokens) {
            parts.push(`<span class="text-xs text-terminal-gray font-mono">👁️ ${Formatters.number(turn.total_visible_tokens)} tokens</span>`);
        }
        
        // For assistant turns
        if (!isUser) {
            // Model used
            if (turn.model_id) {
                parts.push(`<span class="text-xs text-primary font-mono">🤖 ${turn.model_id}</span>`);
            }
            
            // File changes
            if (turn.total_lines_added > 0 || turn.total_lines_removed > 0) {
                const changes = [];
                if (turn.total_lines_added > 0) changes.push(`<span class="text-terminal-green">+${turn.total_lines_added}</span>`);
                if (turn.total_lines_removed > 0) changes.push(`<span class="text-error">-${turn.total_lines_removed}</span>`);
                parts.push(`<span class="text-xs font-mono">${changes.join(' ')}</span>`);
            }
        }
        
        if (parts.length === 0) return '';
        
        return `
            <div class="mt-3 pt-2 border-t border-border-dark flex flex-wrap gap-2">
                ${parts.join('<span class="text-border-dark">•</span>')}
            </div>
        `;
    },

    /**
     * Render files list for a turn
     */
    _renderFilesList(turn) {
        if (!turn.files || turn.files.length === 0) return '';
        
        const filesId = `files-${turn.turn}`;
        
        if (turn.files.length <= 3) {
            const displayFiles = turn.files.map(f => Formatters.escapeHtml(f.split('/').pop())).join(', ');
            return `
                <div class="mt-2 text-xs text-terminal-gray font-mono">
                    📎 ${turn.files.length} file(s): ${displayFiles}
                </div>
            `;
        }
        
        const allFiles = turn.files.map(f => `
            <div class="py-0.5">📄 ${Formatters.escapeHtml(f.split('/').pop())}</div>
        `).join('');
        
        return `
            <div class="mt-2 text-xs">
                <div class="cursor-pointer hover:bg-surface-dark rounded px-2 py-1 -mx-2 text-terminal-gray flex items-center gap-1" onclick="Turns.toggleFilesList('${filesId}')">
                    <span id="${filesId}-arrow" class="material-symbols-outlined text-[1rem] transition-transform">chevron_right</span>
                    <span>📎 ${turn.files.length} files</span>
                </div>
                <div id="${filesId}-content" class="mt-1 ml-6 text-terminal-gray font-mono" style="display: none;">
                    ${allFiles}
                </div>
            </div>
        `;
    },

    /**
     * Render tools list for a turn
     */
    _renderToolsList(turn) {
        const toolRuns = Array.isArray(turn.tool_runs) ? turn.tool_runs : [];
        if (toolRuns.length === 0) {
            if (!turn.tools || turn.tools.length === 0) return '';
            return `
                <div class="mt-2 text-xs text-purple-400 font-mono">
                    🔧 ${turn.tools.length} tool(s): ${turn.tools.join(', ')}
                </div>
            `;
        }

        const allTools = toolRuns.map(toolRun => {
            const statusHtml = toolRun.status && toolRun.status !== 'unknown'
                ? `<span class="text-[10px] uppercase tracking-wide text-terminal-gray">${Formatters.escapeHtml(toolRun.status)}</span>`
                : '';
            const subagentButton = toolRun.spawned_session_id
                ? `<button type="button" onclick="Turns.openSubagentSession('${toolRun.spawned_session_id}')" class="text-terminal-green hover:text-white">agent</button>`
                : '';
            return `
                <div class="flex items-center justify-between py-1 gap-3">
                    <div class="flex items-center gap-2 min-w-0">
                        <span>🔧</span>
                        <button type="button" onclick="Turns.showToolRunDetails(${turn.turn}, ${toolRun.tool_index})" class="text-left text-purple-400 hover:text-white truncate">${Formatters.escapeHtml(toolRun.name || `tool ${toolRun.tool_index + 1}`)}</button>
                        ${statusHtml}
                    </div>
                    <div class="flex items-center gap-2 text-xs font-mono">
                        ${subagentButton}
                    </div>
                </div>
            `;
        }).join('');

        const toolsId = `tools-${turn.turn}`;
        return `
            <div class="mt-2 text-xs">
                <div class="cursor-pointer hover:bg-surface-dark rounded px-2 py-1 -mx-2 text-purple-400 flex items-center gap-1" onclick="Turns.toggleToolsList('${toolsId}')">
                    <span id="${toolsId}-arrow" class="material-symbols-outlined text-[1rem] transition-transform">chevron_right</span>
                    <span>🔧 ${toolRuns.length} tools</span>
                </div>
                <div id="${toolsId}-content" class="mt-1 ml-6 text-purple-400 font-mono" style="display: none;">
                    ${allTools}
                </div>
            </div>
        `;
    },

    _renderSubagentsList(turn) {
        const subagentRuns = Array.isArray(turn.subagent_runs) ? turn.subagent_runs : [];
        if (subagentRuns.length === 0) return '';

        const subagentsId = `subagents-${turn.turn}`;
        const allSubagents = subagentRuns.map(subagentRun => `
            <div class="flex items-center justify-between py-1 gap-3">
                <div class="flex items-center gap-2 min-w-0">
                    <span>🤖</span>
                    <button type="button" onclick="Turns.showSubagentDetails(${turn.turn}, ${subagentRun.subagent_index})" class="text-left text-terminal-green hover:text-white truncate">${Formatters.escapeHtml(subagentRun.title || subagentRun.subagent_session_id)}</button>
                </div>
                <div class="text-xs font-mono text-terminal-gray whitespace-nowrap">${Formatters.number(subagentRun.turn_count || 0)} turns</div>
            </div>
        `).join('');

        return `
            <div class="mt-2 text-xs">
                <div class="cursor-pointer hover:bg-surface-dark rounded px-2 py-1 -mx-2 text-terminal-green flex items-center gap-1" onclick="Turns.toggleToolsList('${subagentsId}')">
                    <span id="${subagentsId}-arrow" class="material-symbols-outlined text-[1rem] transition-transform">chevron_right</span>
                    <span>🤖 ${subagentRuns.length} subagents</span>
                </div>
                <div id="${subagentsId}-content" class="mt-1 ml-6 text-terminal-green font-mono" style="display: none;">
                    ${allSubagents}
                </div>
            </div>
        `;
    },

    /**
     * Render code edits for a turn
     */
    _renderCodeEdits(turn) {
        if (!turn.code_edits || turn.code_edits.length === 0) {
            if (turn.lines_added > 0 || turn.lines_removed > 0 || turn.files_edited > 0) {
                // Simple inline display matching files/tools style
                const parts = [];
                if (turn.files_edited > 0) parts.push(`${turn.files_edited} file(s)`);
                if (turn.lines_added > 0) parts.push(`<span class="text-terminal-green">+${turn.lines_added}</span>`);
                if (turn.lines_removed > 0) parts.push(`<span class="text-error">-${turn.lines_removed}</span>`);
                return `
                    <div class="mt-2 text-xs text-terminal-gray font-mono">
                        📝 ${parts.join(', ')}
                    </div>
                `;
            }
            return '';
        }
        
        const edits = turn.code_edits;
        const totalAdded = edits.reduce((sum, e) => sum + (e.lines_added || 0), 0);
        const totalRemoved = edits.reduce((sum, e) => sum + (e.lines_removed || 0), 0);
        const editsId = `edits-${turn.turn}`;
        
        const fileList = edits.map(edit => {
            const fileName = edit.file_path ? edit.file_path.split(/[/\\]/).pop() : 'Unknown file';
            const lang = edit.language || '';
            const added = edit.lines_added || 0;
            const removed = edit.lines_removed || 0;
            
            let changeText = '';
            if (added > 0 || removed > 0) {
                const parts = [];
                if (added > 0) parts.push(`<span class="text-terminal-green">+${added}</span>`);
                if (removed > 0) parts.push(`<span class="text-error">-${removed}</span>`);
                changeText = parts.join(' ');
            }
            
            return `
                <div class="flex items-center justify-between py-0.5">
                    <div class="flex items-center gap-2 truncate">
                        <span>📄</span>
                        <span class="truncate" title="${Formatters.escapeHtml(edit.file_path)}">${Formatters.escapeHtml(fileName)}</span>
                        ${lang ? `<span class="text-purple-400">${lang}</span>` : ''}
                    </div>
                    <div class="whitespace-nowrap ml-2">${changeText}</div>
                </div>
            `;
        }).join('');

        // Summary line matching files/tools style
        const summaryParts = [`${edits.length} file(s)`];
        if (totalAdded > 0) summaryParts.push(`<span class="text-terminal-green">+${totalAdded}</span>`);
        if (totalRemoved > 0) summaryParts.push(`<span class="text-error">-${totalRemoved}</span>`);
        
        return `
            <div class="mt-2 text-xs">
                <div class="cursor-pointer hover:bg-surface-dark rounded px-2 py-1 -mx-2 text-terminal-gray font-mono flex items-center gap-1" onclick="Turns.toggleCodeEdits('${editsId}')">
                    <span id="${editsId}-arrow" class="material-symbols-outlined text-[1rem] transition-transform">chevron_right</span>
                    <span>📝 ${summaryParts.join(', ')}</span>
                </div>
                <div id="${editsId}-content" class="mt-1 ml-6 text-terminal-gray font-mono" style="display: none;">
                    ${fileList}
                </div>
            </div>
        `;
    },

    /**
     * Toggle files list visibility
     */
    toggleFilesList(filesId) {
        const content = document.getElementById(`${filesId}-content`);
        const arrow = document.getElementById(`${filesId}-arrow`);
        
        if (content.style.display === 'none') {
            content.style.display = '';
            arrow.style.transform = 'rotate(90deg)';
        } else {
            content.style.display = 'none';
            arrow.style.transform = 'rotate(0deg)';
        }
    },

    /**
     * Toggle code edits visibility
     */
    toggleCodeEdits(editsId) {
        const content = document.getElementById(`${editsId}-content`);
        const arrow = document.getElementById(`${editsId}-arrow`);
        
        if (content.style.display === 'none') {
            content.style.display = '';
            arrow.style.transform = 'rotate(90deg)';
        } else {
            content.style.display = 'none';
            arrow.style.transform = 'rotate(0deg)';
        }
    },

    /**
     * Toggle tools list visibility
     */
    toggleToolsList(toolsId) {
        const content = document.getElementById(`${toolsId}-content`);
        const arrow = document.getElementById(`${toolsId}-arrow`);
        
        if (content.style.display === 'none') {
            content.style.display = '';
            arrow.style.transform = 'rotate(90deg)';
        } else {
            content.style.display = 'none';
            arrow.style.transform = 'rotate(0deg)';
        }
    },

    /**
     * Toggle group collapse state
     */
    toggleGroup(groupId) {
        const content = document.getElementById(`${groupId}-content`);
        const collapsed = document.getElementById(`${groupId}-collapsed`);
        const groupEl = document.getElementById(groupId);
        
        if (!content || !collapsed) return;

        if (content.classList.contains('hidden')) {
            // Expand
            content.classList.remove('hidden');
            collapsed.classList.add('hidden');
        } else {
            // Collapse
            content.classList.add('hidden');
            collapsed.classList.remove('hidden');
        }
    },

    /**
     * Toggle all groups
     * @param {boolean} [forceCollapse] - Optional boolean to force state (true=collapse, false=expand)
     */
    toggleAllGroups(forceCollapse) {
        if (typeof forceCollapse === 'boolean') {
            this.areAllCollapsed = forceCollapse;
        } else {
            this.areAllCollapsed = !this.areAllCollapsed;
        }
        
        // Label update removed since text is gone
        
        const groups = document.querySelectorAll('.turn-group');
        groups.forEach(group => {
            const content = document.getElementById(`${group.id}-content`);
            const collapsed = document.getElementById(`${group.id}-collapsed`);
            if (!content || !collapsed) return;

            if (this.areAllCollapsed) {
                // Collapse
                content.classList.add('hidden');
                collapsed.classList.remove('hidden');
            } else {
                // Expand
                content.classList.remove('hidden');
                collapsed.classList.add('hidden');
            }
        });
    }
};

// Export for use in other modules
window.Turns = Turns;
