# SFT LLaMA-Factory Setup

# install dependencies
pip install -e ".[torch,metrics]"
pip uninstall torch -y
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118 # to fix cuda issue

# download clevr_r1 dataset
cd data/
git lfs install
git clone https://huggingface.co/datasets/hunarbatra/clevr_r1
cd clevr_r1
git lfs pull
mv clevr_r1.json ../
mv clevr_r1_data/ ../
cd ../..
rm -rf data/clevr_r1

# SFT Qwen2.5-VL-3B-Instruct with clevr_r1
llamafactory-cli train examples/train_lora/qwen25vl_lora_sft.yaml