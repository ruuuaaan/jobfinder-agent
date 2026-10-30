import argparse
import multiprocessing
from .storage import DATA


def main():
    parser=argparse.ArgumentParser(description='JobFinder 本机工作台')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    import uvicorn
    from .api import create_app
    from .worker import run_worker
    stop=multiprocessing.Event()
    worker=multiprocessing.Process(target=run_worker,args=(str(DATA),stop),name='JobFinder-worker')
    worker.start()
    try:
        uvicorn.run(create_app(),host='127.0.0.1',port=args.port,log_level='warning',access_log=False)
    finally:
        stop.set()
        worker.join(timeout=10)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=3)


if __name__=='__main__':
    main()
