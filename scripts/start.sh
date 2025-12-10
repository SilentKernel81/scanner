#!/bin/bash
# ============================================================================
# Card Scanner Startup Script
# Easily switch between AI providers and configure settings
# ============================================================================

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${BLUE}================================${NC}"
echo -e "${BLUE}  Card Scanner Startup${NC}"
echo -e "${BLUE}================================${NC}"
echo ""

# Parse command line arguments
PROVIDER=""
MODEL=""
SHOW_HELP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -p|--provider)
            PROVIDER="$2"
            shift 2
            ;;
        -m|--model)
            MODEL="$2"
            shift 2
            ;;
        -h|--help)
            SHOW_HELP=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            SHOW_HELP=true
            shift
            ;;
    esac
done

# Show help
if [ "$SHOW_HELP" = true ]; then
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -p, --provider PROVIDER   Set AI provider (gemini, openai, anthropic, local)"
    echo "  -m, --model MODEL         Set specific model to use"
    echo "  -h, --help                Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0                                 # Start with config.yaml settings"
    echo "  $0 --provider gemini               # Use Gemini (default model)"
    echo "  $0 --provider local --model llava:13b  # Use local Ollama with llava:13b"
    echo "  $0 --provider openai --model gpt-4o    # Use OpenAI GPT-4o"
    echo ""
    echo "Environment variables (set in config.yaml or shell):"
    echo "  GEMINI_API_KEY        - Google Gemini API key"
    echo "  OPENAI_API_KEY        - OpenAI API key"
    echo "  ANTHROPIC_API_KEY     - Anthropic API key"
    echo "  LOCAL_AI_ENDPOINT     - Local AI endpoint URL"
    echo ""
    exit 0
fi

# Set provider if specified
if [ -n "$PROVIDER" ]; then
    export VISION_AI_PROVIDER="$PROVIDER"
    echo -e "${GREEN}✓ AI Provider:${NC} $PROVIDER"
fi

# Set model if specified
if [ -n "$MODEL" ]; then
    export LOCAL_AI_MODEL="$MODEL"
    echo -e "${GREEN}✓ Model:${NC} $MODEL"
fi

# Load environment variables from .env file if it exists
if [ -f "$PROJECT_DIR/.env" ]; then
    echo -e "${GREEN}✓ Loading environment from .env${NC}"
    set -a
    source "$PROJECT_DIR/.env"
    set +a
fi

# Check API keys
echo ""
echo -e "${BLUE}Checking AI Configuration:${NC}"

# Get provider from env or config
CURRENT_PROVIDER="${VISION_AI_PROVIDER:-$(grep 'provider:' "$PROJECT_DIR/config.yaml" 2>/dev/null | awk '{print $2}' || echo 'gemini')}"

case "$CURRENT_PROVIDER" in
    gemini)
        if [ -z "$GEMINI_API_KEY" ]; then
            echo -e "${YELLOW}⚠ Warning: GEMINI_API_KEY not set${NC}"
            echo -e "  Set it in config.yaml or export GEMINI_API_KEY=your_key_here"
        else
            echo -e "${GREEN}✓ Gemini API key found${NC}"
        fi
        ;;
    openai)
        if [ -z "$OPENAI_API_KEY" ]; then
            echo -e "${YELLOW}⚠ Warning: OPENAI_API_KEY not set${NC}"
            echo -e "  Set it in config.yaml or export OPENAI_API_KEY=your_key_here"
        else
            echo -e "${GREEN}✓ OpenAI API key found${NC}"
        fi
        ;;
    anthropic)
        if [ -z "$ANTHROPIC_API_KEY" ]; then
            echo -e "${YELLOW}⚠ Warning: ANTHROPIC_API_KEY not set${NC}"
            echo -e "  Set it in config.yaml or export ANTHROPIC_API_KEY=your_key_here"
        else
            echo -e "${GREEN}✓ Anthropic API key found${NC}"
        fi
        ;;
    local)
        ENDPOINT="${LOCAL_AI_ENDPOINT:-http://localhost:11434}"
        echo -e "${GREEN}✓ Using local AI at:${NC} $ENDPOINT"
        # Test connection
        if curl -s --max-time 2 "$ENDPOINT/api/tags" > /dev/null 2>&1; then
            echo -e "${GREEN}✓ Local AI server is reachable${NC}"
        else
            echo -e "${YELLOW}⚠ Warning: Cannot reach local AI server${NC}"
            echo -e "  Make sure Ollama is running: ollama serve"
        fi
        ;;
esac

# Check for required dependencies
echo ""
echo -e "${BLUE}Checking Dependencies:${NC}"

# Check Python
if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version | awk '{print $2}')
    echo -e "${GREEN}✓ Python:${NC} $PYTHON_VERSION"
else
    echo -e "${RED}✗ Python 3 not found${NC}"
    exit 1
fi

# Check virtual environment
if [ -d "$PROJECT_DIR/venv" ]; then
    echo -e "${GREEN}✓ Virtual environment found${NC}"
    source "$PROJECT_DIR/venv/bin/activate"
else
    echo -e "${YELLOW}⚠ Virtual environment not found${NC}"
    echo -e "  Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
fi

# Check database
if [ -f "$PROJECT_DIR/data/cards_database.db" ]; then
    DB_SIZE=$(du -h "$PROJECT_DIR/data/cards_database.db" | cut -f1)
    echo -e "${GREEN}✓ Card database:${NC} $DB_SIZE"
else
    echo -e "${YELLOW}⚠ Card database not found${NC}"
    echo -e "  Run: python3 setup_database.py"
fi

# Check YAML config
if [ -f "$PROJECT_DIR/config.yaml" ]; then
    echo -e "${GREEN}✓ Config file:${NC} config.yaml"
else
    echo -e "${YELLOW}⚠ config.yaml not found, using defaults${NC}"
fi

# Start the application
echo ""
echo -e "${BLUE}================================${NC}"
echo -e "${GREEN}Starting Card Scanner...${NC}"
echo -e "${BLUE}================================${NC}"
echo ""
echo -e "Web interface: ${GREEN}http://0.0.0.0:5000${NC}"
echo -e "Press Ctrl+C to stop"
echo ""

cd "$PROJECT_DIR"
python3 app.py
