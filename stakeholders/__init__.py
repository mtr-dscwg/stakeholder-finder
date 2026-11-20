"""Find the stakeholders connected to a news article."""

from .analyze import analyze_html, analyze_url
from .models import Report, Stakeholder

__all__ = ["analyze_html", "analyze_url", "Report", "Stakeholder"]
__version__ = "0.1.0"
