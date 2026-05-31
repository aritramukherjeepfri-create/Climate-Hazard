import pandas as pd
import json
import os

# Ensure the data directory exists
os.makedirs('data', exist_ok=True)

# List of scenarios to process
scenarios = ['ssp126', 'ssp245', 'ssp585']

def process_scenarios():
    print("🚀 Starting data conversion...")
    
    for s in scenarios:
        filename = f'chi_tiles_2030_{s}.csv'
        
        # Check if file exists in the current folder
        if os.path.exists(filename):
            print(f"Processing {filename}...")
            df = pd.read_csv(filename)
            
            # Convert the full CSV to JSON (the dashboard needs this for map data)
            json_output = df.to_json(orient='records')
            
            # Save to the data folder
            output_path = f'data/chi_tiles_2030_{s}.json'
            with open(output_path, 'w') as f:
                f.write(json_output)
            print(f"✅ Saved: {output_path}")
        else:
            print(f"⚠️ Warning: {filename} not found in the project folder. Skipping.")

    # Convert metadata once
    if os.path.exists('block_metadata.csv'):
        meta_df = pd.read_csv('block_metadata.csv')
        meta_df.to_json('data/block_metadata.json', orient='records')
        print("✅ Saved: data/block_metadata.json")

    print("🎉 All tasks completed!")

if __name__ == "__main__":
    process_scenarios()