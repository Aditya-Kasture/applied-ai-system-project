"""
VibeFinder AI Agent - natural language music recommendation interface.

Wraps the VibeFinder scoring engine with Claude's tool-use API to create an
agentic workflow:
  1. User types a plain-English request ("something chill for studying")
  2. Claude infers structured preferences and calls get_music_recommendations
  3. Claude receives scored results and explains them conversationally

The intermediate tool call is an observable planning step - the agent decides
what genre/mood/energy to request before seeing any results.

Run interactively:  python -m src.agent
"""

import os
import sys
import json
import logging
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv

try:
    from recommender import load_songs, recommend_songs, SCORING_MODES
except ModuleNotFoundError:
    from src.recommender import load_songs, recommend_songs, SCORING_MODES

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definition - Claude uses this to structure its preference extraction
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_music_recommendations",
        "description": (
            "Search the VibeFinder song catalog for tracks matching the user's taste. "
            "Call this whenever the user asks for music recommendations. "
            "Infer all parameters from the user's natural language request. "
            "If the user doesn't specify something, use your best judgment based on context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "genre": {
                    "type": "string",
                    "description": (
                        "Preferred genre. Available: pop, lofi, rock, ambient, jazz, "
                        "synthwave, indie pop, r&b, electronic, country, hip-hop, metal, "
                        "reggae, k-pop, folk"
                    ),
                },
                "mood": {
                    "type": "string",
                    "description": (
                        "Desired mood. Available: happy, chill, intense, relaxed, "
                        "moody, focused, hype"
                    ),
                },
                "energy": {
                    "type": "number",
                    "description": (
                        "Energy level 0.0-1.0. "
                        "Low 0.2-0.4 = calm/study, medium 0.5-0.7 = balanced, "
                        "high 0.8-1.0 = workout/hype"
                    ),
                },
                "likes_acoustic": {
                    "type": "boolean",
                    "description": "True if user prefers acoustic or unplugged sounds.",
                },
                "target_popularity": {
                    "type": "integer",
                    "description": "0-100. Low = underground/indie, high = mainstream. Default 50.",
                },
                "preferred_decade": {
                    "type": "integer",
                    "description": "2010 or 2020 for era preference, or 0 for none.",
                },
                "favorite_mood_tags": {
                    "type": "string",
                    "description": (
                        "Comma-separated vibe tags from: euphoric, nostalgic, peaceful, "
                        "focused, calm, dreamy, mysterious, retro, warm, uplifting, bright, "
                        "aggressive, driving, dark"
                    ),
                },
                "scoring_mode": {
                    "type": "string",
                    "enum": ["balanced", "genre_first", "mood_first", "energy_focused"],
                    "description": (
                        "Weight preset. Use genre_first when genre matters most, "
                        "mood_first when mood drives the request, energy_focused for "
                        "workout/intensity use cases, balanced otherwise."
                    ),
                },
                "k": {
                    "type": "integer",
                    "description": "Number of recommendations to return. Default 5.",
                },
                "diversity_penalty": {
                    "type": "number",
                    "description": (
                        "0.0-1.0. Values above 0 reduce repeated artist/genre clusters. "
                        "Use 0.3-0.5 when the user wants variety."
                    ),
                },
            },
            "required": ["genre", "mood", "energy", "likes_acoustic"],
        },
    }
]

SYSTEM_PROMPT = """You are VibeFinder, a friendly AI music recommendation assistant.

The catalog has 18 songs spanning these genres: pop, lofi, rock, ambient, jazz, \
synthwave, indie pop, r&b, electronic, country, hip-hop, metal, reggae, k-pop, folk.
Available moods: happy, chill, intense, relaxed, moody, focused, hype.

Workflow when a user asks for music:
1. Think about what they want: genre, mood, energy level, any special vibes.
2. Call get_music_recommendations with your best inferred parameters.
3. Present the results in a friendly, conversational way with brief reasons.

For follow-up requests ("make it more acoustic", "something from the 2010s"), \
update parameters and call the tool again.

Be honest: the catalog is small (18 songs) and uses exact string matching, so \
sometimes there are no perfect matches. Say so rather than overselling weak picks.

--- Response format examples (follow this tone and structure) ---

Example A
User: "something calm for studying"
[calls tool: genre=lofi, mood=focused, energy=0.35, scoring_mode=mood_first]
Response style: Lead with the use-case context ("great for focused work"), list \
each song with genre tag and one concrete reason tied to the user's request, \
note catalog limits honestly if the fifth pick is a stretch.

Example B
User: "high energy workout music"
[calls tool: genre=metal, mood=intense, energy=0.95, scoring_mode=energy_focused]
Response style: Open with the top energy match and its energy value, name 3-5 \
tracks with intensity notes, flag if only one true genre match exists.

Example C (follow-up refinement)
User: "can you make it less pop?"
[calls tool again with adjusted genre, same other params, diversity_penalty=0.3]
Response style: Acknowledge the change ("adjusted away from pop"), explain what \
shifted in the results, keep it to 2-3 sentences of context before the list.

--- End examples ---"""


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _execute_recommendation_tool(tool_input: dict, songs: list) -> str:
    """Run the recommendation engine and return a JSON string of results."""
    prefs = {
        "genre": tool_input.get("genre", ""),
        "mood": tool_input.get("mood", ""),
        "energy": float(tool_input.get("energy", 0.5)),
        "likes_acoustic": bool(tool_input.get("likes_acoustic", False)),
        "target_popularity": int(tool_input.get("target_popularity", 50)),
        "preferred_decade": int(tool_input.get("preferred_decade", 0)),
        "favorite_mood_tags": tool_input.get("favorite_mood_tags", ""),
        "scoring_mode": tool_input.get("scoring_mode", "balanced"),
    }
    k = int(tool_input.get("k", 5))
    diversity_penalty = float(tool_input.get("diversity_penalty", 0.0))

    logger.info(
        "Tool call → genre=%s mood=%s energy=%.2f mode=%s k=%d penalty=%.1f",
        prefs["genre"], prefs["mood"], prefs["energy"],
        prefs["scoring_mode"], k, diversity_penalty,
    )

    recs = recommend_songs(prefs, songs, k=k, diversity_penalty=diversity_penalty)

    results = []
    for song, score, explanation in recs:
        results.append({
            "title": song["title"],
            "artist": song["artist"],
            "genre": song["genre"],
            "mood": song["mood"],
            "energy": song["energy"],
            "score": round(score, 2),
            "top_reason": explanation.split(";")[0].strip(),
        })

    logger.info("Engine returned %d results (top score=%.2f)", len(results),
                results[0]["score"] if results else 0)
    return json.dumps(results, indent=2)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class MusicAgent:
    """
    Agentic wrapper around VibeFinder using Claude tool use for natural language I/O.

    The agent loop:
      user message → Claude (decides what to search) → tool call → results
      → Claude (explains results) → response to user

    Multi-turn: history is preserved across calls so the user can refine requests.
    """

    def __init__(self, songs_path: str = "data/songs.csv"):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY not set. "
                "Copy .env.example to .env and add your key."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.songs = load_songs(songs_path)
        self.history: list = []
        logger.info("MusicAgent ready — %d songs loaded", len(self.songs))

    def chat(self, user_message: str) -> str:
        """Process a user message and return the agent's conversational response."""
        self.history.append({"role": "user", "content": user_message})
        logger.info("User: %s", user_message[:80])

        # Agentic loop: keep going until Claude stops using tools
        while True:
            response = self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=self.history,
            )

            if response.stop_reason == "tool_use":
                # Append assistant's tool-planning message to history
                self.history.append({"role": "assistant", "content": response.content})

                # Execute each requested tool and collect results
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info("Agent calling tool: %s", block.name)
                        if block.name == "get_music_recommendations":
                            result_str = _execute_recommendation_tool(
                                block.input, self.songs
                            )
                        else:
                            result_str = json.dumps({"error": f"Unknown tool: {block.name}"})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })

                # Feed results back into the conversation
                self.history.append({"role": "user", "content": tool_results})

            elif response.stop_reason == "end_turn":
                text_parts = [b.text for b in response.content if hasattr(b, "text")]
                reply = "\n".join(text_parts).strip()
                self.history.append({"role": "assistant", "content": response.content})
                logger.info("Agent replied (%d chars)", len(reply))
                return reply

            else:
                logger.warning("Unexpected stop_reason: %s", response.stop_reason)
                return "I ran into an unexpected issue. Please try again."

    def reset(self) -> None:
        """Clear conversation history to start a fresh session."""
        self.history = []
        logger.info("Conversation history cleared")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("\nVibeFinder AI Agent")
    print("=" * 50)
    print("Tell me what music you're in the mood for.")
    print("Commands: 'reset' to start over, 'quit' to exit.\n")

    songs_path = "data/songs.csv"
    if not Path(songs_path).exists():
        songs_path = str(Path(__file__).parent.parent / "data" / "songs.csv")

    try:
        agent = MusicAgent(songs_path=songs_path)
    except EnvironmentError as exc:
        print(f"Setup error: {exc}")
        sys.exit(1)

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"quit", "exit"}:
            print("Goodbye!")
            break
        if user_input.lower() == "reset":
            agent.reset()
            print("Conversation reset.\n")
            continue

        try:
            reply = agent.chat(user_input)
            print(f"\nVibeFinder: {reply}\n")
        except anthropic.AuthenticationError:
            print("Invalid API key. Check your .env file.")
        except anthropic.RateLimitError:
            print("Rate limit hit. Wait a moment and try again.")
        except anthropic.APIError as exc:
            logger.error("API error: %s", exc)
            print(f"API error: {exc}\n")


if __name__ == "__main__":
    main()
