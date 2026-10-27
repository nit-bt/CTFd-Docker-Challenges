# Crash-test image: every instance holds ~230 MB and burns its CPU share. NOT a real challenge.
#   sudo docker build -t ctf/stress .
FROM python:3.12-alpine
COPY hog.py /hog.py
EXPOSE 80
CMD ["python", "-u", "/hog.py"]
