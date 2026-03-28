SELECT
  person.email AS EMAIL
FROM PERSON
GROUP BY
  person.email
HAVING
  COUNT(person.email) > 1