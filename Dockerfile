# Imagen oficial de AWS para Lambda con Python 3.12.
# Ya trae el runtime interface client, por eso solo hay que copiar el codigo.
FROM public.ecr.aws/lambda/python:3.12

# Las dependencias van primero para aprovechar la cache de capas de Docker.
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

COPY app ${LAMBDA_TASK_ROOT}/app

# Modulo.funcion que Lambda invoca en cada request.
CMD ["app.main.handler"]
