/**
 * Declarative Dashboard Renderer
 * 
 * Renders dashboards based on YAML configurations from the server.
 * Supports metrics, charts, and lists with configurable data sources.
 */

console.log('[DashboardRenderer] Module starting to load...');

// Import chart rendering modules
import { 
    escapeHtml, 
    formatMetricValue, 
    getColorClass, 
    getIcon 
} from '../charts/utils.js';

import { 
    renderChartCard as renderStandardChartCard, 
    initializeCharts as initializeStandardCharts 
} from '../charts/chart-renderer.js';

import { 
    renderHeatmap, 
    initializeHeatmaps 
} from '../charts/heatmap-renderer.js';

import { 
    renderWordCloudCard, 
    initializeWordClouds 
} from '../charts/wordcloud-renderer.js';

import { renderDataTable, toggleTableSort } from '../charts/table-renderer.js';
import { renderQuotesCarousel } from '../charts/carousel-renderer.js';

// ==================== Dashboard State ====================

let dashboardConfigs = {};  // Cached dashboard configurations
let dashboardData = {};     // Cached dashboard data
let dashboardCharts = {};   // Active Chart.js instances
let resizeTimeout = null;   // Debounce timer for resize operations

// ==================== Dashboard Loader ====================




/**
 * Load dashboard data for a workspace or system
 */
async function loadDashboardData(workspaceId, dashboardId) {
    try {
        // Use system endpoint if workspaceId is null/undefined or 'system'
        const url = (!workspaceId || workspaceId === 'system')
            ? `/api/system/dashboards/${dashboardId}`
            : `/api/browse/workspace/${workspaceId}/dashboards/${dashboardId}`;
        
        const response = await fetch(url);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        dashboardData[dashboardId] = data;
        return data;
    } catch (error) {
        console.error(`Failed to load dashboard data for ${dashboardId}:`, error);
        return null;
    }
}

// ==================== Dashboard Rendering ====================

/**
 * Render a complete dashboard from declarative config
 * @param {string} containerId - ID of the container element
 * @param {string|null} workspaceIdOrNull - Workspace ID, or null/'system' for system dashboards
 * @param {string} dashboardId - Dashboard ID
 * @returns {object|null} The dashboard data object (includes is_available), or null on error
 */
async function renderDeclarativeDashboard(containerId, workspaceIdOrNull, dashboardId) {
    const container = document.getElementById(containerId);
    if (!container) {
        console.error(`Dashboard container not found: ${containerId}`);
        return null;
    }

    // Show loading state
    container.innerHTML = `
        <div class="flex items-center justify-center py-12">
            <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary"></div>
            <span class="ml-3 text-terminal-gray font-mono">Loading dashboard...</span>
        </div>
    `;

    // Load dashboard data
    const data = await loadDashboardData(workspaceIdOrNull, dashboardId);
    
    if (!data) {
        container.innerHTML = `
            <div class="text-center py-12 text-terminal-gray">
                <p class="font-mono">Failed to load dashboard</p>
            </div>
        `;
        return null;
    }
    
    if (!data.is_available) {
        container.innerHTML = `
            <div class="text-center py-12 text-terminal-gray">
                <p class="font-mono">${escapeHtml(data.message || 'Dashboard not available')}</p>
            </div>
        `;
        return data;
    }
    
    const config = data.config;
    const dashData = data.data;
    
    // Build dashboard HTML
    let html = '';
    
    // Render metrics row
    if (config.metrics && config.metrics.length > 0) {
        html += renderMetricsRow(config.metrics, dashData.metrics);
    }
    
    // Render charts grid
    if (config.charts && config.charts.length > 0) {
        html += renderChartsGrid(config.charts, dashData.charts, dashboardId);
    }
    
    container.innerHTML = html;
    
    // Initialize charts after DOM is updated
    setTimeout(async () => {
        await initializeCharts(config.charts, dashData.charts, dashboardId);
        initializeHeatmaps();
        loadDeferredCharts(config.charts, dashData.charts, dashboardId, workspaceIdOrNull);
        
        // Setup table sort toggle handlers
        setupTableSortHandlers(container, containerId, workspaceIdOrNull, dashboardId);
    }, 0);
    
    return data;
}

/**
 * Render metrics row
 */
function renderMetricsRow(metricsConfig, metricsData) {
    const metrics = metricsConfig.map(metric => {
        const value = metricsData[metric.id];
        const formattedValue = formatMetricValue(value, metric.format);
        const colorClass = getColorClass(metric.color);
        // Support text_size: xs, sm, md, lg, xl (default is md)
        const sizeClass = metric.text_size ? `size-${metric.text_size}` : '';
        
        // Support subtitle_field: renders a smaller secondary line below the value
        let subtitleHtml = '';
        if (metric.subtitle_field && metricsData[metric.id + '_subtitle']) {
            subtitleHtml = `<div class="metric-subtitle">${escapeHtml(metricsData[metric.id + '_subtitle'])}</div>`;
        }
        
        return `
            <div class="metric-card">
                ${metric.icon ? `<span class="metric-icon material-symbols-outlined">${getIcon(metric.icon)}</span>` : ''}
                <p class="metric-label">${escapeHtml(metric.title)}</p>
                <div class="metric-value ${colorClass} ${sizeClass}">${formattedValue}</div>
                ${subtitleHtml}
                ${metric.description ? `<p class="text-xs text-terminal-gray mt-1 font-mono">${escapeHtml(metric.description)}</p>` : ''}
            </div>
        `;
    }).join('');
    
    return `
        <div class="metrics-grid mb-6">
            ${metrics}
        </div>
    `;
}

/**
 * Render charts grid
 */
function renderChartsGrid(chartsConfig, chartsData, dashboardId) {
    // Filter out charts with no data
    const validCharts = chartsConfig.filter(chart => {
        const data = chartsData[chart.id];
        return data && (Array.isArray(data) ? data.length > 0 : Object.keys(data).length > 0);
    });
    
    if (validCharts.length === 0) {
        return '';
    }
    
    const charts = validCharts.map(chart => {
        const data = chartsData[chart.id];
        return renderChartCard(chart, data, dashboardId);
    }).join('');
    
    return `
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
            ${charts}
        </div>
    `;
}

/**
 * Render a single chart card
 */
function renderChartCard(chartConfig, chartData, dashboardId) {
    const chartType = chartConfig.chart_type || 'bar';
    
    // Route to appropriate renderer
    if (chartType === 'quotes') {
        return renderQuotesCarousel(chartConfig, chartData);
    }
    if (chartType === 'table') {
        return renderDataTable(chartConfig, chartData, dashboardId);
    }
    if (chartType === 'heatmap') {
        return renderHeatmap(chartConfig, chartData, dashboardId);
    }
    if (chartType === 'word_cloud') {
        return renderWordCloudCard(chartConfig, chartData, dashboardId);
    }
    
    // Standard Chart.js charts
    return renderStandardChartCard(chartConfig, chartData, dashboardId);
}

// ==================== Chart Initialization ====================

/**
 * Initialize all charts (delegates to modular renderers)
 */
async function initializeCharts(chartsConfig, chartsData, dashboardId) {
    // Initialize standard Chart.js charts
    await initializeStandardCharts(chartsConfig, chartsData, dashboardId, dashboardCharts);
    
    // Initialize word clouds
    await initializeWordClouds(chartsConfig, chartsData, dashboardId);
}

async function loadDeferredCharts(chartsConfig, chartsData, dashboardId, workspaceIdOrNull) {
    const deferredCharts = chartsConfig.filter(chart => {
        const data = chartsData[chart.id];
        return data && data.deferred;
    });

    if (deferredCharts.length === 0) return;

    await Promise.all(deferredCharts.map(async chartConfig => {
        try {
            const url = (!workspaceIdOrNull || workspaceIdOrNull === 'system')
                ? `/api/system/dashboards/${dashboardId}/charts/${chartConfig.id}`
                : null;

            if (!url) return;

            const response = await fetch(url);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);

            const payload = await response.json();
            chartsData[chartConfig.id] = payload.data;
            await initializeWordClouds([chartConfig], chartsData, dashboardId);
        } catch (error) {
            console.error(`Failed to load deferred chart ${chartConfig.id}:`, error);
            const chartId = `chart-${dashboardId}-${chartConfig.id}`;
            const canvas = document.getElementById(chartId);
            const loading = canvas?.parentElement?.querySelector('.wordcloud-loading');
            if (loading) {
                loading.innerHTML = 'Failed to load word cloud';
            }
        }
    }));
}

// Attach global resize listener for heatmaps
window.addEventListener('resize', () => {
    clearTimeout(resizeTimeout);
    resizeTimeout = setTimeout(() => {
        initializeHeatmaps();
    }, 100);
});

// ==================== Table Sort Toggle Handlers ====================

/**
 * Setup click handlers for table sort toggle buttons
 */
function setupTableSortHandlers(container, containerId, workspaceIdOrNull, dashboardId) {
    const toggleButtons = container.querySelectorAll('.table-sort-toggle');
    
    toggleButtons.forEach(button => {
        button.addEventListener('click', async () => {
            const tableId = button.dataset.tableId;
            if (!tableId) return;
            
            // Toggle the sort state
            if (toggleTableSort(tableId)) {
                // Re-render the entire dashboard to reflect new sort
                await renderDeclarativeDashboard(containerId, workspaceIdOrNull, dashboardId);
            }
        });
    });
}

// ==================== Public API ====================

console.log('[DashboardRenderer] About to register window.DashboardRenderer...');

// Export functions for use in workspace.js
window.DashboardRenderer = {    
    //loadDashboardData,
    renderDeclarativeDashboard,
    clearCache: () => {
        dashboardConfigs = {};
        dashboardData = {};
    }
};

console.log('[DashboardRenderer] window.DashboardRenderer registered:', !!window.DashboardRenderer);

// Dispatch ready event for other scripts to know when DashboardRenderer is available
window.dispatchEvent(new CustomEvent('DashboardRendererReady'));
console.log('DashboardRenderer loaded and ready');
