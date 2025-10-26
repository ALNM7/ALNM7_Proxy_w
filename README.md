# WEB SCRAPPER 

## Requirements

- Python 3.10+
- Google Chrome installed

## How to run
1. Create a virtual environment 

```bash
 python -m venv environment_name
```
2. Activate the virtual environment
```bash
    #On windows: 
   environtment_name/Scripts/activate.bat
```

3. Install the requirements.txt 
```bash
    pip install -r requirements.txt
```

4. Run the code

```bash
# Worker 0
python crawlerProxy_fixed.py --dataset-start 1 --dataset-end 20 --workers 4 --worker-id 0 --proxy-set 0 &
# Worker 1
python crawlerProxy_fixed.py --dataset-start 1 --dataset-end 20 --workers 4 --worker-id 1 --proxy-set 1 &
# Worker 2
python crawlerProxy_fixed.py --dataset-start 1 --dataset-end 20 --workers 4 --worker-id 2 --proxy-set 2 &
# Worker 3
python crawlerProxy_fixed.py --dataset-start 1 --dataset-end 20 --workers 4 --worker-id 3 --proxy-set 3 &
wait

```
Run each one of them on a different terminal.