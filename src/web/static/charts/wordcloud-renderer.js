/**
 * Word cloud renderer
 * Canvas-based word cloud visualization
 */

import { defaultPalette, escapeHtml } from './utils.js';
import { ensureScriptLoaded } from './script-loader.js';

const WORDCLOUD_SRC = 'https://cdnjs.cloudflare.com/ajax/libs/wordcloud2.js/1.0.1/wordcloud2.js';

/**
 * Render word cloud card HTML
 */
export function renderWordCloudCard(chartConfig, chartData, dashboardId) {
    const widthClass = chartConfig.width === 'full' ? 'md:col-span-2' : '';
    const chartId = `chart-${dashboardId}-${chartConfig.id}`;
    const selectId = `wordcloud-select-${dashboardId}-${chartConfig.id}`;
    const toggleId = `wordcloud-toggle-${dashboardId}-${chartConfig.id}`;
    const options = chartConfig.options || {};

    const groups = (chartData && chartData.groups) ? chartData.groups : [];
    const defaultGroupId = (chartData && chartData.default_group_id) ? chartData.default_group_id : (groups[0]?.id || '');
    const isDeferred = Boolean(chartData && chartData.deferred);

    const selectHtml = groups.length > 0
        ? `
            <select id="${selectId}" class="bg-background-dark text-terminal-gray border border-border-dark rounded px-2 py-1 text-xs font-mono">
                ${groups.map(g => {
                    const selected = (g.id === defaultGroupId) ? 'selected' : '';
                    return `<option value="${escapeHtml(String(g.id))}" ${selected}>${escapeHtml(String(g.label))}</option>`;
                }).join('')}
            </select>
        `
        : '';
    
    const toggleHtml = `
        <div id="${toggleId}" class="hidden items-center gap-1 bg-background-dark border border-border-dark rounded px-2 py-1">
            <button class="wordcloud-toggle-btn active px-2 py-0.5 text-xs font-mono rounded transition-colors" data-mode="response">Response</button>
            <button class="wordcloud-toggle-btn px-2 py-0.5 text-xs font-mono rounded transition-colors" data-mode="thinking">Thinking</button>
        </div>
    `;

    return `
        <div class="dashboard-card ${widthClass}">
            <div class="card-header mb-4">
                <div class="flex-1">
                    <h3 class="card-title">
                        <span class="material-symbols-outlined">cloud</span>
                        ${escapeHtml(chartConfig.title)}
                    </h3>
                    ${chartConfig.description ? `<p class="text-xs text-terminal-gray mt-1 font-mono">${escapeHtml(chartConfig.description)}</p>` : ''}
                </div>
                <div class="flex items-center gap-2">
                    ${selectHtml}
                    ${toggleHtml}
                </div>
            </div>
            <div style="position: relative; height: 500px; width: 100%; display: flex; align-items: stretch; justify-content: stretch;">
                ${isDeferred ? `
                    <div class="wordcloud-loading absolute inset-0 flex items-center justify-center text-terminal-gray font-mono text-sm">
                        <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-primary mr-3"></div>
                        Loading word cloud...
                    </div>
                ` : ''}
                <canvas id="${chartId}" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%; display: block;"></canvas>
            </div>
        </div>
    `;
}

/**
 * Initialize word cloud visualization
 */
export function initializeWordCloud(canvas, chartConfig, payload, dashboardId) {
    const chartKey = `${dashboardId}-${chartConfig.id}`;
    const selectId = `wordcloud-select-${dashboardId}-${chartConfig.id}`;
    const toggleId = `wordcloud-toggle-${dashboardId}-${chartConfig.id}`;
    const select = document.getElementById(selectId);
    const toggle = document.getElementById(toggleId);
    const options = chartConfig.options || {};

    const groups = payload.groups || [];
    const defaultGroupId = payload.default_group_id || (groups[0]?.id || '');
    
    let currentMode = 'response';

    function getListForGroup(groupId, mode) {
        const groupData = (payload.word_lists && payload.word_lists[groupId]) ? payload.word_lists[groupId] : null;
        
        // Handle new structure: {response: [...], thinking: [...]}
        if (groupData && typeof groupData === 'object' && !Array.isArray(groupData)) {
            const list = groupData[mode] || [];
            return Array.isArray(list) ? list : [];
        }
        
        // Fallback for old structure (flat array)
        return Array.isArray(groupData) ? groupData : [];
    }

    function updateToggleVisibility(groupId) {
        if (!toggle) return;
        
        // Show toggle only for assistant groups
        if (groupId.startsWith('assistant')) {
            toggle.classList.remove('hidden');
        } else {
            toggle.classList.add('hidden');
        }
    }

    function draw(groupId) {
        const list = getListForGroup(groupId, currentMode);
        updateToggleVisibility(groupId);

        const ctx = canvas.getContext('2d');
        
        const container = canvas.parentElement;
        const rect = container.getBoundingClientRect();
        
        if (rect.width === 0 || rect.height === 0) {
            setTimeout(() => draw(groupId), 100);
            return;
        }
        
        const w = Math.max(1, Math.floor(rect.width * window.devicePixelRatio));
        const h = Math.max(1, Math.floor(rect.height * window.devicePixelRatio));
        
        canvas.width = w;
        canvas.height = h;
        ctx.clearRect(0, 0, w, h);

        if (!window.WordCloud) {
            ctx.save();
            ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
            ctx.fillStyle = '#92a4c9';
            ctx.font = '14px JetBrains Mono, monospace';
            ctx.fillText('WordCloud library not loaded', 12, 24);
            ctx.restore();
            return;
        }

        const weights = list.map(pair => pair[1]).filter(v => Number.isFinite(v));
        const maxWeight = weights.length ? Math.max(...weights) : 1;
        const minWeight = weights.length ? Math.min(...weights.filter(v => v > 0)) : 1;

        const gridSize = Math.max(6, Math.round(8 * (rect.width / 1200)));
        const minSize = Math.max(12, Math.floor(rect.height / 40));
        const maxSize = Math.max(70, Math.floor(rect.height / 6));

        window.WordCloud(canvas, {
            list: list,
            gridSize: gridSize,
            shape: 'square',
            weightFactor: (w) => {
                const t = maxWeight > minWeight ? ((w - minWeight) / (maxWeight - minWeight)) : 0;
                return minSize + (Math.pow(t, 0.8) * (maxSize - minSize));
            },
            fontFamily: 'JetBrains Mono, monospace',
            color: () => defaultPalette[Math.floor(Math.random() * defaultPalette.length)],
            backgroundColor: 'transparent',
            rotateRatio: 0.3,
            rotationSteps: 4,
            drawOutOfBound: false,
            shrinkToFit: false,
            minSize: Math.floor(minSize * 0.7),
            wait: 0,
        });
    }

    const initial = (select && select.value) ? select.value : defaultGroupId;
    
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            draw(initial);
        });
    });

    if (select) {
        select.onchange = () => {
            currentMode = 'response';
            
            if (toggle) {
                const buttons = toggle.querySelectorAll('.wordcloud-toggle-btn');
                buttons.forEach(btn => {
                    if (btn.dataset.mode === 'response') {
                        btn.classList.add('active');
                    } else {
                        btn.classList.remove('active');
                    }
                });
            }
            
            draw(select.value);
        };
    }
    
    if (toggle) {
        const buttons = toggle.querySelectorAll('.wordcloud-toggle-btn');
        buttons.forEach(btn => {
            btn.onclick = () => {
                const mode = btn.dataset.mode;
                if (mode === currentMode) return;
                
                currentMode = mode;
                
                buttons.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                
                const groupId = (select && select.value) ? select.value : defaultGroupId;
                draw(groupId);
            };
        });
    }

    let resizeTimer = null;
    window.addEventListener('resize', () => {
        if (resizeTimer) clearTimeout(resizeTimer);
        resizeTimer = setTimeout(() => {
            const groupId = (select && select.value) ? select.value : defaultGroupId;
            draw(groupId);
        }, 150);
    }, { passive: true });
}

/**
 * Initialize all word clouds on the page
 */
export async function initializeWordClouds(chartsConfig, chartsData, dashboardId) {
    const hasWordCloud = chartsConfig.some(c => {
        const chartType = c.chart_type || 'bar';
        const data = chartsData[c.id];
        return chartType === 'word_cloud' && data && data.word_lists;
    });
    if (hasWordCloud) {
        try {
            await ensureScriptLoaded(WORDCLOUD_SRC, 'WordCloud');
        } catch (error) {
            console.error('WordCloud2 failed to load:', error);
            // Continue: initializeWordCloud will show a fallback message.
        }
    }

    chartsConfig.forEach(chartConfig => {
        const chartType = chartConfig.chart_type || 'bar';
        if (chartType !== 'word_cloud') return;

        const chartId = `chart-${dashboardId}-${chartConfig.id}`;
        const canvas = document.getElementById(chartId);
        if (!canvas) return;

        const data = chartsData[chartConfig.id];
        if (!data || !data.word_lists) return;

        const loading = canvas.parentElement?.querySelector('.wordcloud-loading');
        if (loading) loading.remove();

        initializeWordCloud(canvas, chartConfig, data, dashboardId);
    });
}
