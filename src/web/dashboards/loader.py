"""
Dashboard Configuration Loader

Loads and parses declarative YAML dashboard configurations.
Supports server functions as data sources - call Python functions to get data.
All chart display configuration (chart_type, colors, labels, options)
is defined in the dashboard YAML itself.
"""

import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

from src.shared.logging.logger import get_logger

logger = get_logger(__name__)


class DataSourceType(str, Enum):
    """Types of data sources for dashboard elements."""
    FUNCTION = "function"
    TASK = "task"


class ChartType(str, Enum):
    """Supported chart types."""
    PIE = "pie"
    DOUGHNUT = "doughnut"
    BAR = "bar"
    HORIZONTAL_BAR = "horizontal_bar"
    LINE = "line"
    STACKED_BAR_TIMELINE = "stacked_bar_timeline"
    STACKED_BAR = "stacked_bar"
    AREA = "area"
    QUOTES = "quotes"
    TABLE = "table"
    SCATTER = "scatter"
    HEATMAP = "heatmap"
    WORD_CLOUD = "word_cloud"
    NONE = "none"


class FormatType(str, Enum):
    """Format types for metric values."""
    NUMBER = "number"
    PERCENT = "percent"
    TEXT = "text"
    DATETIME = "datetime"
    DATETIME_SPLIT = "datetime:split"
    DATETIME_COMPACT = "datetime:compact"


@dataclass
class DataSource:
    """Configuration for a data source."""
    type: DataSourceType
    function: Optional[str] = None
    field: Optional[str] = None
    task_file: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DataSource":
        return cls(
            type=DataSourceType(data.get("type", "function")),
            function=data.get("function"),
            field=data.get("field"),
            task_file=data.get("task_file"),
        )


@dataclass
class MetricConfig:
    """Configuration for a metric/statistic display."""
    id: str
    title: str
    data_source: DataSource
    description: str = ""
    format: FormatType = FormatType.NUMBER
    color: Optional[str] = None
    icon: Optional[str] = None
    text_size: Optional[str] = None  # xs, sm, md, lg, xl
    subtitle_field: Optional[str] = None  # Field name for subtitle from data source
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MetricConfig":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            data_source=DataSource.from_dict(data.get("data_source", {})),
            format=FormatType(data.get("format", "number")),
            color=data.get("color"),
            icon=data.get("icon"),
            text_size=data.get("text_size"),
            subtitle_field=data.get("subtitle_field"),
        )


@dataclass
class ChartDataset:
    """Configuration for a chart dataset (for multi-line charts)."""
    field: str
    label: str
    color: str
    fill: bool = False
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChartDataset":
        return cls(
            field=data["field"],
            label=data["label"],
            color=data.get("color", "#3b82f6"),
            fill=data.get("fill", False),
        )


@dataclass
class ChartConfig:
    """Configuration for a chart."""
    id: str
    data_source: DataSource
    title: str = ""
    description: str = ""
    chart_type: ChartType = ChartType.BAR
    width: str = "half"  # "half" or "full"
    value_field: Optional[str] = None
    label_field: Optional[str] = None
    x_field: Optional[str] = None
    y_field: Optional[str] = None
    datasets: List[ChartDataset] = field(default_factory=list)
    colors: Dict[str, str] = field(default_factory=dict)
    options: Dict[str, Any] = field(default_factory=dict)
    columns: Optional[List[Dict[str, Any]]] = None  # For table charts with custom columns
    labels: Optional[Dict[str, Dict[str, str]]] = None  # Custom labels and descriptions
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChartConfig":
        datasets = []
        if "datasets" in data:
            datasets = [ChartDataset.from_dict(ds) for ds in data["datasets"]]
        
        return cls(
            id=data["id"],
            title=data.get("title", ""),
            description=data.get("description", ""),
            data_source=DataSource.from_dict(data.get("data_source", {})),
            chart_type=ChartType(data.get("chart_type", "bar")) if "chart_type" in data else ChartType.BAR,
            width=data.get("width", "half"),
            value_field=data.get("value_field"),
            label_field=data.get("label_field"),
            x_field=data.get("x_field"),
            y_field=data.get("y_field"),
            datasets=datasets,
            colors=data.get("colors", {}),
            options=data.get("options", {}),
            columns=data.get("columns"),
            labels=data.get("labels"),
        )


@dataclass
class ListConfig:
    """Configuration for a list display."""
    id: str
    title: str
    data_source: DataSource
    description: str = ""
    label_field: str = "label"
    value_field: str = "value"
    max_items: int = 10
    display_type: str = "list"  # "list" or "progress_bar"
    color: str = "blue"
    show_total: bool = False
    columns: Optional[List[Dict[str, Any]]] = None  # For table display with columns
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ListConfig":
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            data_source=DataSource.from_dict(data.get("data_source", {})),
            label_field=data.get("label_field", "label"),
            value_field=data.get("value_field", "value"),
            max_items=data.get("max_items", 10),
            display_type=data.get("display_type", "list"),
            color=data.get("color", "blue"),
            show_total=data.get("show_total", False),
            columns=data.get("columns")  # Parse columns if present
        )


@dataclass
class DashboardMeta:
    """Dashboard metadata."""
    id: str
    name: str
    description: str = ""
    icon: str = ""
    is_system_level: bool = False
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DashboardMeta":
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            icon=data.get("icon", ""),
            is_system_level=data.get("is_system_level", False),
        )


@dataclass
class DashboardConfig:
    """Complete dashboard configuration."""
    dashboard: DashboardMeta
    metrics: List[MetricConfig] = field(default_factory=list)
    charts: List[ChartConfig] = field(default_factory=list)
    source_file: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_file: Optional[str] = None) -> "DashboardConfig":
        metrics = [MetricConfig.from_dict(m) for m in data.get("metrics", [])]
        charts = [ChartConfig.from_dict(c) for c in data.get("charts", [])]
        
        return cls(
            dashboard=DashboardMeta.from_dict(data["dashboard"]),
            metrics=metrics,
            charts=charts,
            source_file=source_file,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "dashboard": {
                "id": self.dashboard.id,
                "name": self.dashboard.name,
                "description": self.dashboard.description,
                "icon": self.dashboard.icon,
                "is_system_level": self.dashboard.is_system_level,
            },
            "metrics": [
                {
                    "id": m.id,
                    "title": m.title,
                    "description": m.description,
                    "format": m.format.value,
                    "color": m.color,
                    "icon": m.icon,
                    "text_size": m.text_size,
                    "subtitle_field": m.subtitle_field,
                    "data_source": {
                        "type": m.data_source.type.value,
                        "function": m.data_source.function,
                        "field": m.data_source.field,
                    }
                }
                for m in self.metrics
            ],
            "charts": [
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "chart_type": c.chart_type.value,
                    "width": c.width,
                    "value_field": c.value_field,
                    "label_field": c.label_field,
                    "x_field": c.x_field,
                    "y_field": c.y_field,
                    "datasets": [
                        {"field": ds.field, "label": ds.label, "color": ds.color, "fill": ds.fill}
                        for ds in c.datasets
                    ],
                    "colors": c.colors,
                    "options": c.options,
                    "columns": c.columns,
                    "data_source": {
                        "type": c.data_source.type.value,
                        "function": c.data_source.function,
                        "task_file": c.data_source.task_file,
                    },
                    "labels": c.labels,
                }
                for c in self.charts
            ],
        }


class DashboardLoader:
    """Loads and manages dashboard configurations."""
    
    def __init__(self, dashboards_dir: Optional[Path] = None, project_root: Optional[Path] = None):
        if dashboards_dir is None:
            # Default to the directory containing this file (src/web/dashboards)
            dashboards_dir = Path(__file__).parent
        self.dashboards_dir = Path(dashboards_dir)
        self.project_root = project_root or Path(__file__).parent.parent.parent
        self._cache: Dict[str, DashboardConfig] = {}
        self._file_mtimes: Dict[str, float] = {}  # Track file modification times
    
    def load_dashboard(self, dashboard_id: str) -> Optional[DashboardConfig]:
        """Load a single dashboard configuration by ID."""
        config_path = self.dashboards_dir / f"{dashboard_id}.yaml"
        if not config_path.exists():
            return None
        
        # Check if file has been modified since last load
        current_mtime = config_path.stat().st_mtime
        cached_mtime = self._file_mtimes.get(dashboard_id)
        
        if dashboard_id in self._cache and cached_mtime == current_mtime:
            return self._cache[dashboard_id]
        
        # File changed or not cached - reload
        config = self._load_config_file(config_path)
        if config:
            self._cache[dashboard_id] = config
            self._file_mtimes[dashboard_id] = current_mtime
        
        return config
    
    def load_all_dashboards(self) -> List[DashboardConfig]:
        """Load all dashboard configurations from the dashboards directory."""
        dashboards = []
        
        if not self.dashboards_dir.exists():
            return dashboards
        
        for yaml_file in sorted(self.dashboards_dir.glob("*.yaml")):
            config = self._load_config_file(yaml_file)
            if config:
                self._cache[config.dashboard.id] = config
                dashboards.append(config)
        
        return dashboards
    
    def _load_config_file(self, path: Path) -> Optional[DashboardConfig]:
        """Load a dashboard configuration from a YAML file."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            
            if not data or "dashboard" not in data:
                return None
            
            return DashboardConfig.from_dict(data, source_file=str(path))
        
        except Exception as e:
            logger.warning("Error loading dashboard config from %s: %s", path, e)
            return None
    
    def clear_cache(self) -> None:
        """Clear the configuration cache."""
        self._cache.clear()
    
    def get_dashboard_ids(self) -> List[str]:
        """Get list of available dashboard IDs."""
        if not self.dashboards_dir.exists():
            return []
        
        return [p.stem for p in sorted(self.dashboards_dir.glob("*.yaml"))]


# Singleton instance
_loader: Optional[DashboardLoader] = None


def get_dashboard_loader() -> DashboardLoader:
    """Get or create the singleton dashboard loader."""
    global _loader
    if _loader is None:
        _loader = DashboardLoader()
    return _loader


def load_dashboard(dashboard_id: str) -> Optional[DashboardConfig]:
    """Convenience function to load a dashboard."""
    return get_dashboard_loader().load_dashboard(dashboard_id)


def load_all_dashboards() -> List[DashboardConfig]:
    """Convenience function to load all dashboards."""
    return get_dashboard_loader().load_all_dashboards()
