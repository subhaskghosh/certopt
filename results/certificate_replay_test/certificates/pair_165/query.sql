SELECT
  person.email
FROM PERSON
GROUP BY
  person.email
HAVING
  COUNT(email) > 1