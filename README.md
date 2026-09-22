# CAD Tutor Backend

FastAPI backend powering the CAD Tutor platform. This service now generates AI-assisted CAD models based on user descriptions, performing geometry classification, feasibility analysis, and tutorial generation.

## Features
- FastAPI server with structured routing
- AI-driven geometry and feature generation
- Model creation pipeline that outputs STL/STEP files
- Description specificity and feasibility scoring
- SQLite database for order logging and admin retrieval
- Secure file upload and download handling
- Email confirmation system for user orders

## Model Generation Workflow
1. User submits a part description via the frontend.
2. Backend interprets the description using AI logic.
3. Geometry and feature plans are generated.
4. A preliminary CAD model is created and stored.
5. The model is returned to the frontend for visualization.

## Tech Stack
- Python
- FastAPI
- SQLite
- OpenAI API
- SolidWorks / Siemens NX (for validation)
- SMTP (Gmail)

## Purpose
This backend enables automated CAD model generation and tutorial creation, forming the core of the CAD Tutor platform.
