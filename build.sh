#!/usr/bin/env bash
set -o errexit

echo "🔧 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "📋 Copying block_metadata.csv to data/..."
mkdir -p data
cp block_metadata.csv data/block_metadata.csv

echo "📦 Running data pipeline (export_for_web.py)..."
python export_for_web.py --data-dir ./data --tiles-dir .

echo "✅ Build complete."
