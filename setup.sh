# SFT LLaMA-Factory Setup
# git clone --depth 1 https://github.com/hiyouga/LLaMA-Factory.git
# cd LLaMA-Factory

# install dependencies
pip install -e ".[torch,metrics]"
pip uninstall torch -y
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118 # to fix cuda issue

# download clevr_r1 dataset
git lfs install
git clone https://huggingface.co/datasets/hunarbatra/clevr_r1
cd clevr_r1
git lfs pull

# SFT Qwen2.5-VL-3B-Instruct with clevr_r1
llamafactory-cli train examples/train_lora/qwen25vl_lora_sft.yaml