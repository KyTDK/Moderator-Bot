from nsfw_detector import predict
model = predict.load_model('./nsfw_mobilenet2.224x224.h5')

# Predict single image
predict.classify(model, r"C:\Users\taber\Downloads\23164412.gif")