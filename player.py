import sys
import subprocess

if __name__ == "__main__":
    filepath = sys.argv[1]
    subprocess.run(['paplay', filepath])
