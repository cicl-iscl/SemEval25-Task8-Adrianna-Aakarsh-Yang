import argparse
from datasets import Dataset, DatasetDict, Features, Value
import os
import json

# Argument Parsing Setup
def parse_args():
    parser = argparse.ArgumentParser(description="Prepare and upload Hugging Face dataset with splits.")
    parser.add_argument("--root_dir", type=str, required=True, help="Root directory containing test cases.")
    parser.add_argument("--output_dir", type=str, required=True, help="Directory to save the Hugging Face dataset.")
    parser.add_argument("--push_to_hub", action="store_true", help="Push the dataset to Hugging Face Hub.")
    parser.add_argument("--repo_name", type=str, help="Hugging Face Hub repository name.")
    return parser.parse_args()

# Function to find test cases and assign splits correctly
def find_saved_prompts(root_dir, split):
    prompts = []
    root_dir=f"{root_dir}/{split}-prompts"
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith(".py") and file.startswith("prompt"):
                prompts.append({
                    'id': file.replace(".py", "").split("_")[-1], # prompt_<id>.py
                    'path': os.path.join(root, file),
                    'split': split
                })
    return prompts

# Streaming generator for test cases (memory efficient)
def test_case_generator(test_cases):
    for details in test_cases:
        try:
            with open(details['path'], 'r') as f:
                content = f.read()
            yield {
                'id': details['id'],
                'split': details['split'],
                'content': content
            }
        except Exception as e:
            print(f"Error reading file {details['path']}: {e}")

# Hugging Face schema definition
features = Features({
    "id": Value("string"),
    "split": Value("string"),
    "content": Value("string")
})

def main():
    args = parse_args()
    root_dir = args.root_dir
    # Prepare datasets for multiple splits
    prompts_train = find_saved_prompts(root_dir, "train")
    prompts_dev = find_saved_prompts(root_dir, "dev")

    
    all_prompts = {
        "train": prompts_train,
        "dev": prompts_dev
    }

    # Create a DatasetDict with streaming datasets
    dataset_dict = DatasetDict({
        split: Dataset.from_generator(lambda s=split: test_case_generator(all_prompts[s]), features=features)
        for split in all_prompts
    })
    
    # Save the dataset locally or push to Hugging Face Hub based on the flag
    if args.push_to_hub:
        if args.repo_name:
            # dataset_dict.push_to_hub(args.repo_name)
            print(f"Dataset successfully pushed to the Hugging Face Hub: {args.repo_name}")
        else:
            print("Error: Please provide a repository name with --repo_name when using --push_to_hub.")
    else:
        dataset_dict.save_to_disk(args.output_dir)
        print(f"Dataset successfully saved to {args.output_dir}")
   
if __name__ == "__main__":
    # python push_prompts.py --root_dir "/content/drive/MyDrive/TUE-WINTER-2024/CHALLENGES-CL/" --push_to_hub --repo_name "aakarsh-nair/semeval-2025-task-8-prompts"
    main()

