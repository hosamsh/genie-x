/**
 * Browse Chats - Workspace listing functionality
 */

// State
let allWorkspaces = [];
let filteredWorkspaces = [];
let currentPage = 1;
let pageSize = 50;
let totalCount = 0;
let totalPages = 1;

// DOM Elements
const loadingEl = document.getElementById('loading');
const errorEl = document.getElementById('error');
const emptyEl = document.getElementById('empty');
const gridEl = document.getElementById('workspace-grid');
const paginationEl = document.getElementById('pagination');

// Initialize
document.addEventListener('DOMContentLoaded', async () => {
    // Wait for AgentInfo to load if available
    if (window.AgentInfo && !window.AgentInfo.loaded) {
        await window.AgentInfo.init();
    }
    populateAgentFilterOptions();
    loadWorkspaces();
});

function getAgentFilterValue() {
    const agentFilter = document.getElementById('agent-filter');
    return agentFilter?.value || 'all';
}

function getSortValue() {
    const sortFilter = document.getElementById('sort-filter');
    return sortFilter?.value || 'name';
}

function buildWorkspacesQuery() {
    const params = new URLSearchParams({
        page: String(currentPage),
        page_size: String(pageSize),
        include_live: 'true',
        sort: getSortValue(),
    });

    const agentFilter = getAgentFilterValue();
    if (agentFilter !== 'all') {
        params.set('agent', agentFilter);
    }

    return params.toString();
}

function populateAgentFilterOptions() {
    const agentFilter = document.getElementById('agent-filter');
    if (!agentFilter) {
        return;
    }

    const selectedAgent = agentFilter.value || 'all';
    const optionsById = new Map();

    if (window.AgentInfo?.agents) {
        for (const [agentId, info] of Object.entries(window.AgentInfo.agents)) {
            if (agentId === 'default') {
                continue;
            }
            optionsById.set(agentId, {
                id: agentId,
                name: info.name || agentId,
            });
        }
    }

    for (const workspace of allWorkspaces) {
        for (const agentId of workspace.agents || []) {
            if (!optionsById.has(agentId)) {
                const info = window.AgentInfo?.get(agentId);
                optionsById.set(agentId, {
                    id: agentId,
                    name: info?.name || agentId,
                });
            }
        }
    }

    const sortedOptions = Array.from(optionsById.values()).sort((left, right) =>
        left.name.localeCompare(right.name)
    );

    agentFilter.innerHTML = '';

    const allOption = document.createElement('option');
    allOption.value = 'all';
    allOption.textContent = 'All Agents';
    agentFilter.appendChild(allOption);

    for (const optionInfo of sortedOptions) {
        const option = document.createElement('option');
        option.value = optionInfo.id;
        option.textContent = optionInfo.name;
        agentFilter.appendChild(option);
    }

    agentFilter.value = optionsById.has(selectedAgent) ? selectedAgent : 'all';
}

/**
 * Load workspaces from API
 */
async function loadWorkspaces() {
    showLoading();
    
    try {
        const response = await fetch(`/api/browse/workspaces?${buildWorkspacesQuery()}`);
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }
        
        const data = await response.json();
        allWorkspaces = data.workspaces;
        totalCount = data.total_count;
        totalPages = data.total_pages;

        populateAgentFilterOptions();
        
        filterWorkspaces();
        
    } catch (error) {
        console.error('Failed to load workspaces:', error);
        showError(error.message);
    }
}

/**
 * Refresh workspaces list
 */
function refreshWorkspaces() {
    currentPage = 1;
    loadWorkspaces();
}

function applyAgentFilter() {
    currentPage = 1;
    loadWorkspaces();
}

function applySort() {
    currentPage = 1;
    loadWorkspaces();
}

/**
 * Filter workspaces based on search and status
 */
function filterWorkspaces() {
    const searchInput = document.getElementById('search-input');
    const searchTerm = searchInput.value.toLowerCase();
    const statusFilter = document.getElementById('status-filter').value;
    
    // Show/hide clear button
    const clearBtn = document.getElementById('search-clear');
    if (clearBtn) {
        if (searchTerm && searchTerm.trim() !== '') {
            clearBtn.classList.remove('hidden');
        } else {
            clearBtn.classList.add('hidden');
        }
    }
    
    filteredWorkspaces = allWorkspaces.filter(ws => {
        // Search filter
        const matchesSearch = !searchTerm || 
            ws.workspace_name.toLowerCase().includes(searchTerm) ||
            ws.workspace_id.toLowerCase().includes(searchTerm) ||
            ws.workspace_folder.toLowerCase().includes(searchTerm);
        
        // Status filter
        let matchesStatus = true;
        if (statusFilter !== 'all') {
            const hasExtracted = Object.values(ws.status).some(s => s.extracted_at);
            
            switch (statusFilter) {
                case 'extracted':
                    matchesStatus = hasExtracted;
                    break;
                case 'none':
                    matchesStatus = !hasExtracted;
                    break;
            }
        }
        
        return matchesSearch && matchesStatus;
    });
    
    renderWorkspaces();
}

/**
 * Render workspaces grid
 */
function renderWorkspaces() {
    if (filteredWorkspaces.length === 0) {
        if (allWorkspaces.length === 0) {
            showEmpty();
        } else {
            // Show "no matches" state
            gridEl.innerHTML = `
                <div class="col-span-full text-center py-8 text-terminal-gray font-mono">
                    No workspaces match your filters
                </div>
            `;
            showGrid();
        }
        return;
    }
    
    gridEl.innerHTML = filteredWorkspaces.map(ws => renderWorkspaceCard(ws)).join('');
    showGrid();
    updatePagination();
    updateStats();
}

/**
 * Render a single workspace card
 */
function renderWorkspaceCard(ws) {
    // Check overall status across all agents
    const isExtracted = Object.values(ws.status).some(s => s.extracted_at);
    
    // Status badges - terminal style
    let statusBadges = '';
    if (isExtracted) {
        statusBadges += `<span class="status-badge bg-primary/20 text-primary border border-primary/30 rounded px-2 py-0.5 text-xs font-mono">✓ EXTRACTED</span>`;
    } else {
        statusBadges = `<span class="status-badge bg-surface-dark text-terminal-gray border border-border-dark rounded px-2 py-0.5 text-xs font-mono">NOT EXTRACTED</span>`;
    }
    
    // Agent badges - use AgentInfo for dynamic colors and icons if available
    const agentBadges = ws.agents.map(agent => {
        let colorClass = 'bg-gray-500/20 text-gray-400 border-gray-500/30'; // default
        let agentIcon = '';
        let agentName = agent;
        
        // Try to get info from AgentInfo if loaded
        if (window.AgentInfo?.loaded) {
            const info = window.AgentInfo.get(agent);
            if (info) {
                // Get display name
                agentName = info.name || agent;
                
                // Get icon
                agentIcon = `<img src="${info.logo}" alt="${agentName}" class="w-3 h-3 object-contain inline-block mr-1" onerror="this.style.display='none'">`;
                
                // Get color
                if (info.color) {
                    const colorMap = {
                        'bg-purple-100 text-purple-700': 'bg-purple-500/20 text-purple-400 border-purple-500/30',
                        'bg-green-100 text-green-700': 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
                        'bg-orange-100 text-orange-700': 'bg-orange-500/20 text-orange-400 border-orange-500/30',
                    };
                    colorClass = colorMap[info.color] || colorClass;
                }
            }
        }
        
        return `<span class="text-xs px-2 py-0.5 ${colorClass} border rounded font-mono flex items-center gap-1 inline-flex" title="${agentName}">${agentIcon}${agentName}</span>`;
    }).join('');
    
    // Format extraction date if available
    let extractionInfo = '';
    const firstExtracted = Object.values(ws.status).find(s => s.extracted_at);
    if (firstExtracted) {
        const date = new Date(firstExtracted.extracted_at);
        extractionInfo = `
            <div class="text-xs text-terminal-gray/70 mt-2 font-mono">
                LATEST: ${date.toLocaleDateString()} ${date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
            </div>
        `;
    }
    
    // Source availability indicator
    const sourceIndicator = ws.source_available === false ? 
        `<span class="text-xs text-warning font-mono" title="Source no longer available on disk">⚠ ARCHIVED</span>` : '';
    
    // Get search term for highlighting
    const searchTerm = document.getElementById('search-input').value;
    const highlightedName = highlightText(ws.workspace_name || ws.workspace_id, searchTerm);
    const highlightedFolder = highlightText(ws.workspace_folder || 'Unknown location', searchTerm);
    
    return `
        <a href="/workspace/${ws.workspace_id}" class="dashboard-card block hover:border-primary/40 transition-all cursor-pointer">
            <div class="flex items-start justify-between mb-2">
                <h3 class="font-semibold text-white truncate flex-1 font-mono" title="${ws.workspace_name}">
                    ${highlightedName}
                </h3>
            </div>
            
            <p class="text-xs text-terminal-gray truncate mb-3 font-mono" title="${ws.workspace_folder}">
                ${highlightedFolder} ${sourceIndicator}
            </p>
            
            <div class="flex items-center justify-between mb-2">
                <div class="flex flex-wrap gap-1">
                    ${agentBadges}
                </div>
                <span class="text-xs text-terminal-gray font-mono">${ws.session_count} SESSIONS</span>
            </div>
            
            <div class="flex flex-wrap gap-1">
                ${statusBadges}
            </div>
            
            ${extractionInfo}
        </a>
    `;
}

/**
 * Update pagination controls
 */
function updatePagination() {
    const start = (currentPage - 1) * pageSize + 1;
    const end = Math.min(currentPage * pageSize, totalCount);
    
    document.getElementById('showing-range').textContent = `${start}-${end}`;
    document.getElementById('total-count').textContent = totalCount;
    document.getElementById('page-info').textContent = `Page ${currentPage} of ${totalPages}`;
    
    document.getElementById('prev-btn').disabled = currentPage <= 1;
    document.getElementById('next-btn').disabled = currentPage >= totalPages;
    
    paginationEl.classList.toggle('hidden', totalPages <= 1);
}

/**
 * Go to previous page
 */
function prevPage() {
    if (currentPage > 1) {
        currentPage--;
        loadWorkspaces();
    }
}

/**
 * Go to next page
 */
function nextPage() {
    if (currentPage < totalPages) {
        currentPage++;
        loadWorkspaces();
    }
}

// UI State helpers
function showLoading() {
    loadingEl.classList.remove('hidden');
    errorEl.classList.add('hidden');
    emptyEl.classList.add('hidden');
    gridEl.classList.add('hidden');
    paginationEl.classList.add('hidden');
}

function showError(message) {
    loadingEl.classList.add('hidden');
    errorEl.classList.remove('hidden');
    emptyEl.classList.add('hidden');
    gridEl.classList.add('hidden');
    paginationEl.classList.add('hidden');
    document.getElementById('error-message').textContent = message;
}

function showEmpty() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    emptyEl.classList.remove('hidden');
    gridEl.classList.add('hidden');
    paginationEl.classList.add('hidden');
}

function showGrid() {
    loadingEl.classList.add('hidden');
    errorEl.classList.add('hidden');
    emptyEl.classList.add('hidden');
    gridEl.classList.remove('hidden');
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/**
 * Highlight search term in text (case-insensitive)
 */
function highlightText(text, searchTerm) {
    if (!searchTerm || searchTerm.trim() === '') return escapeHtml(text);
    
    const escaped = escapeHtml(text);
    const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(${escapedTerm})`, 'gi');
    
    return escaped.replace(regex, '<span class="search-highlight">$1</span>');
}

/**
 * Clear search input
 */
function clearSearch() {
    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.value = '';
        searchInput.blur();
    }
    filterWorkspaces();
}

/**
 * Update stats summary in the header
 */
function updateStats() {
    const statsEl = document.getElementById('stats-summary');
    const bulkBarEl = document.getElementById('bulk-extraction-bar');
    if (!statsEl || !bulkBarEl) {
        console.warn('Stats or Bulk Extraction Bar element is missing.');
        return;
    }

    // Count extracted workspaces (those with any extraction status)
    const extractedCount = allWorkspaces.filter(ws => 
        Object.values(ws.status).some(s => s.extracted_at)
    ).length;

    const totalCount = allWorkspaces.length;
    const remainingCount = totalCount - extractedCount;

    // Update stats display elements
    const extractedCountEl = document.getElementById('extracted-count');
    const totalWorkspacesEl = document.getElementById('total-workspaces');
    const bulkSubtitleEl = document.getElementById('bulk-extraction-subtitle');
    const extractRemainingBtn = document.getElementById('extract-remaining-btn');
    const extractAllBtn = document.getElementById('extract-all-btn');

    if (extractedCountEl) extractedCountEl.textContent = extractedCount;
    if (totalWorkspacesEl) totalWorkspacesEl.textContent = totalCount;

    // Case: no discovered workspaces at all
    if (totalCount === 0) {
        statsEl.classList.add('hidden');
        bulkBarEl.classList.remove('hidden');

        if (bulkSubtitleEl) {
            bulkSubtitleEl.textContent = 'No workspaces found. Navigate to projects below or run a bulk extract to discover and index workspaces.';
        }

        if (extractRemainingBtn) extractRemainingBtn.style.display = 'none';
        if (extractAllBtn) extractAllBtn.style.display = 'inline-block';

        return;
    }

    // Case: workspaces exist but none have been indexed yet
    if (extractedCount === 0) {
        statsEl.classList.add('hidden');
        bulkBarEl.classList.remove('hidden');

        if (bulkSubtitleEl) {
              bulkSubtitleEl.innerHTML = `<strong>Your system shows ${totalCount} workspace${totalCount > 1 ? 's' : ''} available but not indexed yet.</strong><br>Open a workspace to index it, or select Bulk Extract All to index everything.`;
        }

        if (extractRemainingBtn) extractRemainingBtn.style.display = 'none';
        if (extractAllBtn) extractAllBtn.style.display = 'inline-block';

        return;
    }

    // Case: some workspaces indexed, some remaining
    if (remainingCount > 0) {
        statsEl.classList.remove('hidden');
        bulkBarEl.classList.remove('hidden');

        if (bulkSubtitleEl) {
            bulkSubtitleEl.textContent = `${remainingCount} workspace${remainingCount > 1 ? 's' : ''} available but not indexed. Extract remaining to index them.`;
        }

        if (extractRemainingBtn) extractRemainingBtn.style.display = 'inline-block';
        if (extractAllBtn) extractAllBtn.style.display = 'none';

        return;
    }

    // All indexed
    statsEl.classList.remove('hidden');
    bulkBarEl.classList.add('hidden');
    if (extractRemainingBtn) extractRemainingBtn.style.display = 'none';
    if (extractAllBtn) extractAllBtn.style.display = 'none';
}

/**
 * Extract remaining workspaces (those not yet extracted)
 */
async function extractRemaining() {
    const remaining = allWorkspaces.filter(ws => 
        !Object.values(ws.status).some(s => s.extracted_at)
    );
    
    if (remaining.length === 0) {
        alert('All workspaces are already extracted!');
        return;
    }
    
    if (!confirm(`Extract ${remaining.length} workspace(s)? This may take a while.`)) {
        return;
    }
    
    await startBulkStreamingExtraction(remaining.map(ws => ws.workspace_id), false);
}

/**
 * Re-extract all workspaces (deletes existing data and re-extracts)
 */
async function reextractAll() {
    if (!confirm(`⚠️ Re-extract all ${allWorkspaces.length} workspace(s)?\n\nThis will DELETE all existing extraction data and re-extract from source files.\n\nAre you sure?`)) {
        return;
    }
    
    await startBulkStreamingExtraction(allWorkspaces.map(ws => ws.workspace_id), true);
}

/**
 * Start streaming bulk extraction with modal popup
 */
async function startBulkStreamingExtraction(workspaceIds, refresh = false) {
    // Show the bulk extraction modal
    showBulkExtractionModal(workspaceIds.length);
    
    try {
        const response = await fetch('/api/browse/bulk-extract-stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ workspace_ids: workspaceIds, refresh: refresh })
        });
        
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        
        const result = await response.json();
        
        if (!result.streaming) {
            appendBulkLogLine('✓ No extraction needed', 'success');
            setBulkModalState('completed');
            return;
        }
        
        // Start streaming output to the modal
        streamBulkExtractionOutput(result.run_id);
        
    } catch (error) {
        console.error('Failed to start bulk extraction:', error);
        appendBulkLogLine('Error: Failed to start bulk extraction - ' + error.message, 'error');
        setBulkModalState('failed');
    }
}

/**
 * Show the bulk extraction modal
 */
function showBulkExtractionModal(totalCount) {
    const modal = document.getElementById('bulk-extraction-modal');
    modal.classList.remove('hidden');
    
    // Clear previous output
    document.getElementById('bulk-output-lines').innerHTML = '';
    document.getElementById('bulk-terminal-cursor').classList.remove('hidden');
    
    // Reset progress
    document.getElementById('bulk-progress-current').textContent = '0';
    document.getElementById('bulk-progress-total').textContent = totalCount;
    
    // Reset modal state
    setBulkModalState('running');
    
    // Update status badge
    const statusEl = document.getElementById('bulk-run-status');
    statusEl.textContent = 'Starting...';
    statusEl.className = 'px-2 py-1 text-xs rounded-full bg-blue-100 text-blue-700';
    
    // Start extraction tips
    if (window.ExtractionTips) {
        ExtractionTips.start('bulk-tips-container', 'bulk-tip-text', 'extraction', 4500);
    }
}

/**
 * Close the bulk extraction modal and refresh
 */
function closeBulkExtractionModal() {
    document.getElementById('bulk-extraction-modal').classList.add('hidden');
    
    // Stop extraction tips
    if (window.ExtractionTips) {
        ExtractionTips.stop('bulk-tips-container');
    }
    
    refreshWorkspaces();
}

/**
 * Set bulk modal footer state (running, completed, failed)
 */
function setBulkModalState(state) {
    document.getElementById('bulk-modal-running').classList.add('hidden');
    document.getElementById('bulk-modal-completed').classList.add('hidden');
    document.getElementById('bulk-modal-failed').classList.add('hidden');
    document.getElementById('bulk-modal-' + state).classList.remove('hidden');
    
    // Hide cursor when not running
    if (state !== 'running') {
        document.getElementById('bulk-terminal-cursor').classList.add('hidden');
        
        // Stop tips when extraction finishes
        if (window.ExtractionTips) {
            ExtractionTips.stop('bulk-tips-container');
        }
    }
}

/**
 * Append a log line to the bulk extraction terminal output
 */
function appendBulkLogLine(message, type = '') {
    const outputLines = document.getElementById('bulk-output-lines');
    const line = document.createElement('div');
    line.className = 'log-line' + (type ? ' ' + type : '');
    line.textContent = message;
    outputLines.appendChild(line);
    
    // Auto-scroll to bottom
    const terminal = document.getElementById('bulk-terminal-output');
    terminal.scrollTop = terminal.scrollHeight;
}

/**
 * Stream bulk extraction output via SSE
 */
function streamBulkExtractionOutput(runId) {
    const statusEl = document.getElementById('bulk-run-status');
    
    statusEl.textContent = 'Running';
    statusEl.className = 'px-2 py-1 text-xs rounded-full bg-blue-100 text-blue-700 animate-pulse';
    
    const eventSource = new EventSource(`/api/run/${runId}/stream`);
    
    eventSource.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'log') {
            // Detect message type for styling
            let lineType = '';
            const msg = data.message.toLowerCase();
            if (msg.includes('error') || msg.includes('failed') || msg.includes('❌')) {
                lineType = 'error';
            } else if (msg.includes('success') || msg.includes('completed') || msg.includes('✓') || msg.includes('✅')) {
                lineType = 'success';
            } else if (msg.includes('===') || msg.includes('---') || msg.startsWith('=')) {
                lineType = 'banner';
            } else if (msg.includes('skipped') || msg.includes('⏭️')) {
                lineType = 'skip';
            } else if (msg.startsWith('[') || msg.includes('📂') || msg.includes('📦') || msg.includes('🔍')) {
                lineType = 'info';
            }
            appendBulkLogLine(data.message, lineType);
        } else if (data.type === 'progress') {
            // Update progress counter
            document.getElementById('bulk-progress-current').textContent = data.current;
            document.getElementById('bulk-progress-total').textContent = data.total;
        } else if (data.type === 'status') {
            if (data.status === 'completed') {
                statusEl.textContent = 'Completed';
                statusEl.className = 'px-2 py-1 text-xs rounded-full bg-green-100 text-green-700';
                setBulkModalState('completed');
            } else if (data.status === 'failed') {
                statusEl.textContent = 'Failed';
                statusEl.className = 'px-2 py-1 text-xs rounded-full bg-red-100 text-red-700';
                setBulkModalState('failed');
            }
        } else if (data.type === 'error') {
            appendBulkLogLine('Error: ' + data.message, 'error');
        } else if (data.type === 'done') {
            eventSource.close();
        }
    };
    
    eventSource.onerror = () => {
        eventSource.close();
    };
}

/**
 * Update the bulk extraction bar based on workspace extraction status
 */
function updateBulkExtractionBar() {
    const bulkBar = document.getElementById('bulk-extraction-bar');
    const subtitle = document.getElementById('bulk-extraction-subtitle');
    const extractRemainingBtn = document.getElementById('extract-remaining-btn');
    const extractAllBtn = document.getElementById('extract-all-btn');

    const unextracted = allWorkspaces.filter(ws => 
        !Object.values(ws.status).some(s => s.extracted_at)
    );

    if (allWorkspaces.length === 0) {
        // No workspaces extracted
        subtitle.textContent = 'No workspaces extracted yet. Navigate to projects or bulk extract all.';
        extractRemainingBtn.style.display = 'none';
        extractAllBtn.style.display = 'block';
    } else if (unextracted.length > 0) {
        // Some workspaces extracted, some not
        subtitle.textContent = `${unextracted.length} workspace(s) unextracted. Extract remaining.`;
        extractRemainingBtn.style.display = 'block';
        extractAllBtn.style.display = 'none';
    } else {
        // All workspaces extracted
        subtitle.textContent = 'All workspaces are already extracted!';
        extractRemainingBtn.style.display = 'none';
        extractAllBtn.style.display = 'none';
    }

    bulkBar.classList.remove('hidden');
}

// Call this function after loading workspace data
updateBulkExtractionBar();

