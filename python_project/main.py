def add(a, b):
    return a + b


def factorial(n):
    if n < 0:
        raise ValueError("factorial is undefined for negative numbers")
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


def is_prime(n):
    if n < 2:
        return False
    i = 2
    while i * i <= n:
        if n % i == 0:
            return False
        i += 1
    return True


def find_max(values):
    if not values:
        raise ValueError("find_max requires a non-empty list")
    max_value = values[0]
    for value in values[1:]:
        if value > max_value:
            max_value = value
    return max_value


def average(values):
    if not values:
        raise ValueError("average requires a non-empty list")
    total = 0
    for value in values:
        total += value
    return total / len(values)


def reverse_string(text):
    chars = list(text)
    left = 0
    right = len(chars) - 1
    while left < right:
        chars[left], chars[right] = chars[right], chars[left]
        left += 1
        right -= 1
    return "".join(chars)


def count_vowels(text):
    count = 0
    for ch in text.lower():
        if ch in "aeiou":
            count += 1
    return count


def main():
    numbers = [3, 41, 7, 19, 25]

    print("add(2, 3) =", add(2, 3))
    print("factorial(5) =", factorial(5))
    print("is_prime(17) =", is_prime(17))
    print("is_prime(18) =", is_prime(18))
    print("find_max =", find_max(numbers))
    print("average =", average(numbers))
    print('reverse_string("hello") =', reverse_string("hello"))
    print('count_vowels("Education") =', count_vowels("Education"))


if __name__ == "__main__":
    main()
