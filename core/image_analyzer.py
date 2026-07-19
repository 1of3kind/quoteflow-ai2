"""AI-powered image analysis for work assessment."""

import base64
import json
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image

logger = logging.getLogger("image_analyzer")


@dataclass
class ImageAnalysis:
    trade: str
    estimated_sqft: Optional[float]
    complexity: str
    damage_level: Optional[str]
    materials_visible: List[str]
    access_difficulty: str
    confidence: float
    notes: str
    suggested_photos: List[str]


class VisionAnalyzer:
    """Analyzes work photos using AI vision (OpenAI GPT-4V or local model)."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or ""
        self.model = "gpt-4-vision-preview"
        self.base_url = "https://api.openai.com/v1/chat/completions"

    async def analyze_image(self, image_path: str, trade: str) -> ImageAnalysis:
        """Analyze a single work photo."""
        encoded = self._encode_image(image_path)
        prompt = self._build_prompt(trade)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert contractor estimator. Analyze the provided work photo and extract structured data for quote generation. Be precise and conservative in estimates.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{encoded}",
                                "detail": "high",
                            },
                        },
                    ],
                },
            ],
            "max_tokens": 800,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(self.base_url, headers=headers, json=payload)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

        return self._parse_response(content, trade)

    async def analyze_batch(self, image_paths: List[str], trade: str) -> ImageAnalysis:
        """Analyze multiple photos and merge results."""
        analyses = []
        for path in image_paths:
            try:
                analysis = await self.analyze_image(path, trade)
                analyses.append(analysis)
            except Exception as e:
                logger.error(f"Failed to analyze {path}: {e}")

        if not analyses:
            return self._default_analysis(trade)

        return self._merge_analyses(analyses, trade)

    def _encode_image(self, image_path: str) -> str:
        """Resize and base64 encode image."""
        img = Image.open(image_path)
        max_size = 1024
        ratio = min(max_size / img.width, max_size / img.height)
        if ratio < 1:
            new_size = (int(img.width * ratio), int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        from io import BytesIO
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        return base64.b64encode(buffer.getvalue()).decode()

    def _build_prompt(self, trade: str) -> str:
        prompts = {
            "landscaping": """Analyze this landscaping photo. Extract:
- Estimated square footage of work area
- Complexity level (low/medium/high)
- Access difficulty (easy/moderate/hard)
- Visible materials needed
- Any problem areas
Return as JSON: {"sqft": number, "complexity": "low|medium|high", "access": "easy|moderate|hard", "materials": [...], "notes": "..."}""",
            "roofing": """Analyze this roofing photo. Extract:
- Estimated roof square footage
- Roof pitch (low/medium/steep)
- Number of shingle layers visible
- Damage level (minor/moderate/major)
Return as JSON: {"roof_sqft": number, "pitch": "low|medium|steep", "layers": number, "damage": "minor|moderate|major", "notes": "..."}""",
            "plumbing": """Analyze this plumbing photo. Extract:
- Number of fixtures involved
- Pipe material visible (copper/pex/pvc)
- Access type (open/wall/slab)
- Severity of issue
Return as JSON: {"fixture_count": number, "pipe_material": "copper|pex|pvc", "access": "open|wall|slab", "emergency": true|false, "notes": "..."}""",
            "autobody": """Analyze this auto body photo. Extract:
- Number of damaged panels
- Damage type (dent/scratch/collision/rust)
- Paint match required (yes/no)
Return as JSON: {"panel_count": number, "damage_type": "dent|scratch|collision|rust", "paint_match": true|false, "notes": "..."}""",
            "electrical": """Analyze this electrical photo. Extract:
- Number of outlets/fixtures
- Amperage (120/240/480)
- Wiring type visible (romex/conduit/bx)
- Permit likely required (yes/no)
Return as JSON: {"outlet_count": number, "amperage": 120|240|480, "wiring_type": "romex|conduit|bx", "permit_required": true|false, "notes": "..."}""",
        }
        return prompts.get(trade, "Analyze this work photo and provide detailed assessment.")

    def _parse_response(self, content: str, trade: str) -> ImageAnalysis:
        """Parse AI response into structured analysis."""
        import re
        json_match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
        if json_match:
            content = json_match.group(1)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            try:
                start = content.find("{")
                end = content.rfind("}") + 1
                data = json.loads(content[start:end])
            except Exception:
                data = {}

        return ImageAnalysis(
            trade=trade,
            estimated_sqft=data.get("sqft") or data.get("roof_sqft"),
            complexity=data.get("complexity", "medium"),
            damage_level=data.get("damage"),
            materials_visible=data.get("materials", []),
            access_difficulty=data.get("access", "moderate"),
            confidence=0.75,
            notes=data.get("notes", ""),
            suggested_photos=[],
        )

    def _merge_analyses(self, analyses: List[ImageAnalysis], trade: str) -> ImageAnalysis:
        """Merge multiple photo analyses into one."""
        complexities = {"low": 1, "medium": 2, "high": 3}
        max_complexity = max(analyses, key=lambda a: complexities.get(a.complexity, 2))

        access_scores = {"easy": 1, "moderate": 2, "hard": 3}
        max_access = max(analyses, key=lambda a: access_scores.get(a.access_difficulty, 2))

        sqfts = [a.estimated_sqft for a in analyses if a.estimated_sqft]
        avg_sqft = sum(sqfts) / len(sqfts) if sqfts else None

        all_materials = []
        for a in analyses:
            all_materials.extend(a.materials_visible)

        damage_levels = {"minor": 1, "moderate": 2, "major": 3}
        analyses_with_damage = [a for a in analyses if a.damage_level]
        max_damage = max(
            analyses_with_damage,
            key=lambda a: damage_levels.get(a.damage_level, 1),
            default=None,
        )

        return ImageAnalysis(
            trade=trade,
            estimated_sqft=avg_sqft,
            complexity=max_complexity.complexity,
            damage_level=max_damage.damage_level if max_damage else None,
            materials_visible=list(set(all_materials)),
            access_difficulty=max_access.access_difficulty,
            confidence=sum(a.confidence for a in analyses) / len(analyses),
            notes=" | ".join(a.notes for a in analyses if a.notes),
            suggested_photos=[],
        )

    def _default_analysis(self, trade: str) -> ImageAnalysis:
        return ImageAnalysis(
            trade=trade,
            estimated_sqft=None,
            complexity="medium",
            damage_level=None,
            materials_visible=[],
            access_difficulty="moderate",
            confidence=0.0,
            notes="Could not analyze images. Manual review required.",
            suggested_photos=[],
        )


class MockAnalyzer(VisionAnalyzer):
    """Returns realistic mock data for testing."""

    async def analyze_image(self, image_path: str, trade: str) -> ImageAnalysis:
        mock_data = {
            "landscaping": ImageAnalysis(
                trade="landscaping",
                estimated_sqft=2500,
                complexity="medium",
                damage_level=None,
                materials_visible=["sod", "mulch"],
                access_difficulty="easy",
                confidence=0.85,
                notes="Front yard needs sod replacement and mulching",
                suggested_photos=["side_yard", "back_yard"],
            ),
            "roofing": ImageAnalysis(
                trade="roofing",
                estimated_sqft=1800,
                complexity="medium",
                damage_level="moderate",
                materials_visible=["asphalt_shingles"],
                access_difficulty="moderate",
                confidence=0.80,
                notes="Missing shingles on south side, some water damage visible",
                suggested_photos=["attic_interior", "gutter_closeup"],
            ),
            "plumbing": ImageAnalysis(
                trade="plumbing",
                estimated_sqft=None,
                complexity="medium",
                damage_level="moderate",
                materials_visible=["copper_pipes"],
                access_difficulty="wall",
                confidence=0.75,
                notes="Leak under kitchen sink, copper pipes corroded",
                suggested_photos=["under_sink_wide", "basement_pipes"],
            ),
        }
        return mock_data.get(trade, ImageAnalysis(
            trade=trade, estimated_sqft=500, complexity="medium",
            damage_level=None, materials_visible=[], access_difficulty="moderate",
            confidence=0.5, notes="Mock analysis", suggested_photos=[],
        ))


def get_analyzer(api_key: Optional[str] = None, mock: bool = False) -> VisionAnalyzer:
    if mock or not api_key:
        return MockAnalyzer()
    return VisionAnalyzer(api_key)
