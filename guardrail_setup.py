# guardrail_setup.py

import re
import boto3
from config import REGION

bedrock = boto3.client("bedrock", region_name=REGION)


def create_guardrail():
    response = bedrock.create_guardrail(
        name        = "nl-query-agent-guardrail",
        description = "Guardrail for NL Data Query Agent — blocks harmful inputs and irrelevant topics",

        # Content filters — blocks hate, insults, violence, misconduct, prompt attacks
        contentPolicyConfig={
            "filtersConfig": [
                {"type": "HATE",          "inputStrength": "HIGH",   "outputStrength": "HIGH"},
                {"type": "INSULTS",       "inputStrength": "HIGH",   "outputStrength": "HIGH"},
                {"type": "VIOLENCE",      "inputStrength": "MEDIUM", "outputStrength": "MEDIUM"},
                {"type": "MISCONDUCT",    "inputStrength": "HIGH",   "outputStrength": "HIGH"},
                {"type": "PROMPT_ATTACK", "inputStrength": "HIGH",   "outputStrength": "NONE"},
            ]
        },

        # Topic denial — agent only answers data-related questions
        topicPolicyConfig={
            "topicsConfig": [
                {
                    "name":       "non-data-topics",
                    "definition": "Any topic not related to data analysis, farmers market data, or Spotify data.",
                    "examples":   [
                        "What is the capital of France?",
                        "Write me a poem",
                        "Help me hack a website",
                        "What stocks should I buy?",
                    ],
                    "type": "DENY",
                }
            ]
        },

        # PII protection — anonymizes emails/phones, blocks AWS keys and passwords
        sensitiveInformationPolicyConfig={
            "piiEntitiesConfig": [
                {"type": "EMAIL",          "action": "ANONYMIZE"},
                {"type": "PHONE",          "action": "ANONYMIZE"},
                {"type": "NAME",           "action": "ANONYMIZE"},
                {"type": "AWS_ACCESS_KEY", "action": "BLOCK"},
                {"type": "PASSWORD",       "action": "BLOCK"},
            ]
        },

        # Grounding check — reduces hallucinations in responses
        contextualGroundingPolicyConfig={
            "filtersConfig": [
                {"type": "GROUNDING", "threshold": 0.7},
                {"type": "RELEVANCE", "threshold": 0.7},
            ]
        },

        blockedInputMessaging   = "⛔ Your question was blocked by safety guardrails. Please ask something related to the farmers market or Spotify datasets.",
        blockedOutputsMessaging = "⛔ The response was blocked by safety guardrails. Please try rephrasing your question.",
    )

    guardrail_id      = response["guardrailId"]
    guardrail_version = response["version"]

    print(f"✅ Guardrail created!")
    print(f"   ID      : {guardrail_id}")
    print(f"   Version : {guardrail_version}")

    # Auto-write ID into config.py — no manual copy-paste needed
    with open("config.py", "r") as f:
        content = f.read()

    content = re.sub(
        r'GUARDRAIL_ID\s*=\s*".*?"',
        f'GUARDRAIL_ID      = "{guardrail_id}"',
        content
    )
    content = re.sub(
        r'GUARDRAIL_VERSION\s*=\s*".*?"',
        f'GUARDRAIL_VERSION = "{guardrail_version}"',
        content
    )

    with open("config.py", "w") as f:
        f.write(content)

    print(f"\n✅ config.py updated automatically — GUARDRAIL_ID set!")
    print(f"   You can now run agent.py directly.")


if __name__ == "__main__":
    print("🛡️  Creating Bedrock Guardrail...\n")
    create_guardrail()